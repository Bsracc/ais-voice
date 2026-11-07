"""Tests for the Twilio Media Streams transport (app/twilio_transport.py).

Covers: TwiML generation, Twilio WebSocket event parsing, the 8 kHz
streaming pipeline build (offline, with fakes), and the official
TwilioFrameSerializer's credential rules. No keys, no network.
"""

import asyncio
import base64
import json
import types

import pytest
from fastapi.testclient import TestClient
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame
from pipecat.serializers.twilio import TwilioFrameSerializer
from starlette.websockets import WebSocketDisconnect

from app import server as server_mod
from app.config import Settings
from app.fakes import FakeLLM, FakeSTT, FakeTTS
from app.twilio_transport import (
    TWILIO_SAMPLE_RATE,
    build_twilio_pipeline,
    build_twiml,
    parse_twilio_message,
    twiml_url_from_stream_url,
)

STREAM_URL = "wss://abc123.ngrok-free.app/twilio/stream"


def _settings(**overrides):
    base = {
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": "claude-sonnet-5-5",
        "DEEPGRAM_API_KEY": "",
        "DEEPGRAM_MODEL": "nova-3",
        "CARTESIA_API_KEY": "",
        "CARTESIA_VOICE_ID": "",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# --- TwiML ------------------------------------------------------------


def test_build_twiml_connects_stream():
    twiml = build_twiml(STREAM_URL)
    assert "<Stream" in twiml
    assert STREAM_URL in twiml
    assert "<Connect>" in twiml
    assert twiml.startswith('<?xml version="1.0" encoding="UTF-8"?>')


def test_build_twiml_rejects_non_wss_url():
    for bad in ("", "https://example.com/twilio/stream", "wss://", "http://x/y"):
        with pytest.raises(ValueError, match="wss://"):
            build_twiml(bad)


def test_build_twiml_escapes_ampersand():
    twiml = build_twiml("wss://host.example/twilio/stream?x=1&y=2")
    assert "&amp;" in twiml
    assert "&y=2" not in twiml


def test_twiml_url_from_stream_url():
    assert (
        twiml_url_from_stream_url(STREAM_URL)
        == "https://abc123.ngrok-free.app/twilio/voice"
    )


def test_twiml_url_rejects_wrong_suffix():
    with pytest.raises(ValueError, match="/twilio/stream"):
        twiml_url_from_stream_url("wss://host.example/other")
    with pytest.raises(ValueError, match="wss://"):
        twiml_url_from_stream_url("https://host.example/twilio/stream")


# --- Twilio WebSocket events -------------------------------------------


def test_parse_start_event():
    raw = json.dumps(
        {
            "event": "start",
            "streamSid": "MZ123",
            "start": {"streamSid": "MZ123", "callSid": "CA456"},
        }
    )
    parsed = parse_twilio_message(raw)
    assert parsed == {"event": "start", "stream_sid": "MZ123", "call_sid": "CA456"}


def test_parse_start_without_stream_sid_is_none():
    raw = json.dumps({"event": "start", "start": {"callSid": "CA456"}})
    assert parse_twilio_message(raw) is None


def test_parse_stop_event():
    assert parse_twilio_message(json.dumps({"event": "stop"})) == {"event": "stop"}


def test_parse_other_events_are_none():
    assert parse_twilio_message(json.dumps({"event": "connected"})) is None
    payload = base64.b64encode(b"\xff" * 160).decode()
    assert (
        parse_twilio_message(
            json.dumps({"event": "media", "media": {"payload": payload}})
        )
        is None
    )
    assert parse_twilio_message("not json at all") is None
    assert parse_twilio_message(b"") is None


# --- 8 kHz streaming pipeline ------------------------------------------


def test_twilio_pipeline_builds_with_fakes_offline():
    settings = _settings()
    built = build_twilio_pipeline(
        settings, stt=FakeSTT(), tts=FakeTTS(), llm=FakeLLM()
    )
    processor_types = [type(p) for p in built.pipeline.processors]
    assert FakeSTT in processor_types
    assert FakeTTS in processor_types
    assert FakeLLM in processor_types
    # Phone call rates: 8 kHz both ways (Twilio is mu-law 8 kHz).
    assert built.worker._params.audio_in_sample_rate == TWILIO_SAMPLE_RATE == 8000
    assert built.worker._params.audio_out_sample_rate == TWILIO_SAMPLE_RATE == 8000


def test_twilio_pipeline_live_factories_require_keys():
    with pytest.raises(ValueError, match="missing API key"):
        build_twilio_pipeline(_settings())


def test_twilio_pipeline_operator_mode_needs_no_anthropic_key():
    # Operator mode builds the streaming STT/TTS (construction only —
    # no network) without any Anthropic key.
    settings = _settings(
        BRAIN_MODE="muse-operator",
        DEEPGRAM_API_KEY="test-dg",
        CARTESIA_API_KEY="test-cart",
        CARTESIA_VOICE_ID="voice-1",
    )
    built = build_twilio_pipeline(settings)
    assert built.llm is None
    assert built.worker._params.audio_in_sample_rate == 8000


# --- official TwilioFrameSerializer ------------------------------------


def test_serializer_without_creds_needs_auto_hangup_off():
    # Default auto_hang_up=True requires call/account/auth (official
    # behavior) — pin it so a misconfigured deploy fails loudly.
    with pytest.raises(ValueError, match="auto_hang_up"):
        TwilioFrameSerializer(stream_sid="MZ1")
    ok = TwilioFrameSerializer(
        stream_sid="MZ1",
        params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
    )
    assert ok is not None


def test_serializer_media_roundtrip():
    serializer = TwilioFrameSerializer(
        stream_sid="MZ1",
        params=TwilioFrameSerializer.InputParams(auto_hang_up=False),
    )
    asyncio.run(serializer.setup(types.SimpleNamespace(audio_in_sample_rate=8000)))
    payload = base64.b64encode(b"\xff" * 160).decode()
    frame = asyncio.run(
        serializer.deserialize(
            json.dumps({"event": "media", "media": {"payload": payload}})
        )
    )
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.sample_rate == 8000
    assert len(frame.audio) > 0
    out = asyncio.run(
        serializer.serialize(
            OutputAudioRawFrame(audio=b"\x00\x00" * 160, sample_rate=8000, num_channels=1)
        )
    )
    assert out is not None
    message = json.loads(out)
    assert message["event"] == "media"
    assert message["streamSid"] == "MZ1"
    assert base64.b64decode(message["media"]["payload"])


# --- server endpoints ----------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    transcript_file = tmp_path / "transcript.jsonl"
    monkeypatch.setattr(server_mod, "TRANSCRIPT_PATH", transcript_file)
    monkeypatch.setattr(server_mod, "transcript_store", None)
    monkeypatch.setattr(server_mod, "KILL_SWITCH_PATH", tmp_path / "kill_switch.json")
    for key in server_mod.REQUIRED_LIVE_KEYS:
        monkeypatch.setenv(key, "")
    for key in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "TWILIO_TO_NUMBER",
        "TWILIO_STREAM_URL",
    ):
        monkeypatch.setenv(key, "")
    server_mod.reset_state()
    with TestClient(server_mod.app) as c:
        yield c
    server_mod.reset_state()


def test_twilio_status_unconfigured_no_secrets(client):
    body = client.get("/api/twilio/status").json()
    assert body["configured"] is False
    assert set(body["missing"]) == {
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "TWILIO_TO_NUMBER",
        "TWILIO_STREAM_URL",
    }
    assert body["test_mode"] is False
    # Key names are fine to list; values must never appear. (Values are
    # empty here; the configured test below checks real values stay out.)


def test_twilio_status_configured(client, monkeypatch):
    for key, value in (
        ("TWILIO_ACCOUNT_SID", "ACtest"),
        ("TWILIO_AUTH_TOKEN", "tok"),
        ("TWILIO_FROM_NUMBER", "+15550001111"),
        ("TWILIO_TO_NUMBER", "+15550100100"),
        ("TWILIO_STREAM_URL", STREAM_URL),
    ):
        monkeypatch.setenv(key, value)
    body = client.get("/api/twilio/status").json()
    assert body["configured"] is True
    assert body["missing"] == []
    # Values are never echoed back.
    assert "ACtest" not in json.dumps(body)
    assert "tok" not in json.dumps(body)


def test_twilio_voice_rejects_when_unconfigured(client):
    r = client.post("/twilio/voice")
    assert r.status_code == 200
    assert "<Reject" in r.text
    assert "application/xml" in r.headers["content-type"]


def test_twilio_voice_returns_twiml_when_configured(client, monkeypatch):
    monkeypatch.setenv("TWILIO_STREAM_URL", STREAM_URL)
    r = client.post("/twilio/voice")
    assert r.status_code == 200
    assert "<Stream" in r.text
    assert STREAM_URL in r.text


def test_twilio_stream_refused_without_keys(client):
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
        "/twilio/stream"
    ):
        pass
    assert exc.value.code == 1013
