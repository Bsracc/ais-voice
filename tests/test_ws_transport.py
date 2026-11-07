"""WebSocket voice transport (/ws/voice): raw-PCM serializer + route.

Run:  .venv/bin/python -m pytest tests/test_ws_transport.py -v
"""

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, TextFrame
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport
from starlette.websockets import WebSocketDisconnect

from app import server as server_mod
from app.fakes import FakeSTT, FakeTTS
from app.pipeline import build_pipeline as _real_build_pipeline
from app.transcript import clear_transcript
from app.ws_transport import (
    AUDIO_IN_SAMPLE_RATE,
    NUM_CHANNELS,
    RawPCMSerializer,
)

# --- serializer ------------------------------------------------------------


def test_deserialize_bytes_to_input_audio_frame():
    pcm = b"\x01\x02" * 320
    frame = asyncio.run(RawPCMSerializer().deserialize(pcm))
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.audio == pcm
    assert frame.sample_rate == AUDIO_IN_SAMPLE_RATE == 16000
    assert frame.num_channels == NUM_CHANNELS == 1


def test_deserialize_bytearray_to_input_audio_frame():
    frame = asyncio.run(RawPCMSerializer().deserialize(bytearray(b"\x00\x01" * 10)))
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.audio == b"\x00\x01" * 10


def test_deserialize_text_is_ignored_not_a_crash():
    # The client may send JSON text such as {"type": "end"}.
    assert asyncio.run(RawPCMSerializer().deserialize('{"type": "end"}')) is None
    assert asyncio.run(RawPCMSerializer().deserialize("")) is None


def test_serialize_output_audio_frame_is_raw_bytes():
    pcm = b"\x03\x04" * 160
    out = asyncio.run(
        RawPCMSerializer().serialize(
            OutputAudioRawFrame(audio=pcm, sample_rate=24000, num_channels=1)
        )
    )
    assert out == pcm


def test_serialize_other_frames_is_none():
    assert asyncio.run(RawPCMSerializer().serialize(TextFrame(text="hi"))) is None


# --- route -----------------------------------------------------------------


@pytest.fixture()
def ws_client(tmp_path, monkeypatch):
    """Fresh app with temp stores + muse-operator STT/TTS keys (no Anthropic)."""
    monkeypatch.setattr(server_mod, "TRANSCRIPT_PATH", tmp_path / "transcript.jsonl")
    monkeypatch.setattr(server_mod, "transcript_store", None)
    monkeypatch.setattr(server_mod, "KILL_SWITCH_PATH", tmp_path / "kill_switch.json")
    monkeypatch.setattr(server_mod, "OPERATOR_PATH", tmp_path / "operator_pending.json")
    monkeypatch.setenv("BRAIN_MODE", "muse-operator")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("CARTESIA_API_KEY", "ct-test")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    server_mod.reset_state()
    clear_transcript()
    with TestClient(server_mod.app) as c:
        yield c
    server_mod.reset_state()
    clear_transcript()


def test_ws_rejected_when_kill_switch_engaged(ws_client):
    from app import killswitch

    killswitch.engage(server_mod.KILL_SWITCH_PATH, note="test")
    with pytest.raises(WebSocketDisconnect) as exc, ws_client.websocket_connect("/ws/voice"):
        pass
    assert exc.value.code == 1013
    # No pipeline was built: status stays idle, nothing to clean up.
    assert ws_client.get("/api/status").json()["state"] == "idle"


def test_ws_rejected_in_demo_mode(ws_client, monkeypatch):
    # No STT/TTS keys: demo mode must close the socket, never build a
    # fake pipeline.
    monkeypatch.setenv("DEEPGRAM_API_KEY", "")
    monkeypatch.setenv("CARTESIA_API_KEY", "")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "")
    with pytest.raises(WebSocketDisconnect) as exc, ws_client.websocket_connect("/ws/voice"):
        pass
    assert exc.value.code == 1013
    assert ws_client.get("/api/status").json()["state"] == "idle"


def test_ws_rejected_when_session_active(ws_client):
    # A WebRTC session owns the single-session slot.
    server_mod._state["state"] = "active"
    server_mod._state["transport"] = "webrtc"
    try:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            ws_client.websocket_connect("/ws/voice"),
        ):
            pass
        assert exc.value.code == 1013
    finally:
        server_mod.reset_state()


def _recv_bytes(ws, timeout=5.0):
    """Receive one binary message, failing (not hanging) on timeout."""
    box = []

    def _run():
        try:
            box.append(ws.receive_bytes())
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller.
            box.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    assert box, f"no bytes received within {timeout}s"
    value = box[0]
    if isinstance(value, Exception):
        raise value
    return value


def _wait_idle(client, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/api/status").json()["state"] == "idle":
            return True
        time.sleep(0.1)
    return False


class _CountingSTT(FakeSTT):
    """FakeSTT that counts the audio frames it received."""

    def __init__(self):
        super().__init__()
        self.audio_frames = 0

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import AudioRawFrame

        if isinstance(frame, AudioRawFrame):
            self.audio_frames += 1
        await super().process_frame(frame, direction)


def test_ws_session_operator_mode_with_fakes(ws_client, monkeypatch):
    """Full loop over /ws/voice with fake STT/TTS: greeting audio out,
    PCM audio in, muse-operator pipeline with no LLM, single-session
    slot enforced while it runs."""
    built = {}

    def _fake_build(settings, transport=None):
        pipeline = _real_build_pipeline(
            settings, stt=_CountingSTT(), tts=FakeTTS(), transport=transport
        )
        built["pipeline"] = pipeline
        return pipeline

    monkeypatch.setattr(server_mod, "build_pipeline", _fake_build)

    # TestClient runs the app in a portal (non-main) thread, where asyncio
    # cannot install signal handlers — WorkerRunner._setup_sigint raises
    # "set_wakeup_fd only works in main thread". Production runs under
    # uvicorn in the main thread, where the runner's SIGINT setup works;
    # the handlers are irrelevant to this test, so they are no-ops here.
    from pipecat.workers.runner import WorkerRunner

    monkeypatch.setattr(WorkerRunner, "_setup_sigint", lambda self: None)
    monkeypatch.setattr(WorkerRunner, "_setup_sigterm", lambda self: None)

    with ws_client.websocket_connect("/ws/voice") as ws:
        status = ws_client.get("/api/status").json()
        assert status["state"] == "active"
        assert status["transport"] == "websocket"
        assert status["brain_mode"] == "muse-operator"

        # A WebRTC offer is refused while the WS session owns the pipeline.
        assert (
            ws_client.post("/api/offer", json={"sdp": "v=0", "type": "offer"}).status_code
            == 503
        )

        # A second WS connection is refused while the first is active.
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            ws_client.websocket_connect("/ws/voice"),
        ):
            pass
        assert exc.value.code == 1013

        # Greeting audio arrives over the socket (TTS -> serializer -> WS).
        greeting = _recv_bytes(ws)
        assert isinstance(greeting, bytes) and len(greeting) > 0

        # Audio in: 5 x 20 ms of PCM16 mono 16 kHz, then a short drain.
        for _ in range(5):
            ws.send_bytes(b"\x00\x00" * 320)
        time.sleep(0.5)

    # After disconnect the session ends and the slot frees.
    assert _wait_idle(ws_client), "session did not go idle after WS disconnect"
    assert ws_client.get("/api/status").json()["transport"] is None

    pipeline = built["pipeline"]
    assert pipeline.llm is None  # muse-operator mode: no LLM processor.
    assert isinstance(pipeline.transport, FastAPIWebsocketTransport)
    assert isinstance(pipeline.stt, _CountingSTT)
    assert pipeline.stt.audio_frames >= 5  # inbound PCM reached the STT.
    # The end-of-session flush wrote the transcribed turns to context:
    # one user message per audio frame, each the fake's fixed sentence.
    user_texts = [
        m["content"] for m in pipeline.context.messages if m.get("role") == "user"
    ]
    assert len(user_texts) == 5
    assert all(text == FakeSTT.FIXED_TEXT for text in user_texts)
