"""REST STT/TTS services: mocked HTTP, real Pipecat frame contracts.

No live network here — every test injects an httpx client backed by
MockTransport. Live format checks (Cartesia raw PCM, Deepgram WAV) were
done once by hand with curl on 7 Nov 2026; see app/rest_services.py.
"""

import asyncio
import wave
from io import BytesIO

import httpx
import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)

from app.config import Settings
from app.fakes import FakeLLM
from app.pipeline import build_pipeline
from app.rest_services import (
    CARTESIA_TTS_BYTES_URL,
    CartesiaRestTTSService,
    DeepgramRestSTTService,
)


def _deepgram_ok_payload(transcript="How much does it cost?"):
    return {
        "results": {
            "channels": [
                {"alternatives": [{"transcript": transcript, "confidence": 0.999}]}
            ]
        }
    }


def _wav_bytes(seconds=1, sample_rate=16000):
    """Tiny mono s16le WAV, as SegmentedSTTService hands segments over."""
    buf = BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * sample_rate * seconds)
    return buf.getvalue()


def _run(coro):
    return asyncio.run(coro)


async def _collect(async_gen):
    return [frame async for frame in async_gen]


# --- Deepgram REST STT ---


def test_stt_returns_transcript_from_mocked_response():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["content_type"] = request.headers["Content-Type"]
        assert request.content == _wav_bytes()
        return httpx.Response(200, json=_deepgram_ok_payload())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stt = DeepgramRestSTTService(api_key="test-deepgram-key", http_client=client)
    frames = _run(_collect(stt.run_stt(_wav_bytes())))

    assert "model=nova-3" in seen["url"]
    assert "language=en" in seen["url"]
    assert "smart_format=true" in seen["url"]
    assert seen["auth"] == "Token test-deepgram-key"
    assert seen["content_type"] == "audio/wav"
    assert len(frames) == 1
    assert isinstance(frames[0], TranscriptionFrame)
    assert frames[0].text == "How much does it cost?"
    assert frames[0].result["results"]["channels"][0]["alternatives"][0][
        "confidence"
    ] == 0.999


def test_stt_empty_transcript_yields_nothing():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_deepgram_ok_payload(""))
        )
    )
    stt = DeepgramRestSTTService(api_key="test-deepgram-key", http_client=client)
    assert _run(_collect(stt.run_stt(_wav_bytes()))) == []


def test_stt_http_error_yields_error_frame_not_crash():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "bad key"})
        )
    )
    stt = DeepgramRestSTTService(api_key="test-deepgram-key", http_client=client)
    frames = _run(_collect(stt.run_stt(_wav_bytes())))
    assert len(frames) == 1
    assert isinstance(frames[0], ErrorFrame)
    assert "401" in frames[0].error


def test_stt_network_failure_yields_error_frame_not_crash():
    def handler(request: httpx.Request):
        raise httpx.ConnectError("proxy refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stt = DeepgramRestSTTService(api_key="test-deepgram-key", http_client=client)
    frames = _run(_collect(stt.run_stt(_wav_bytes())))
    assert len(frames) == 1
    assert isinstance(frames[0], ErrorFrame)


def test_stt_malformed_response_yields_error_frame():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"unexpected": True})
        )
    )
    stt = DeepgramRestSTTService(api_key="test-deepgram-key", http_client=client)
    frames = _run(_collect(stt.run_stt(_wav_bytes())))
    assert len(frames) == 1
    assert isinstance(frames[0], ErrorFrame)


def test_stt_missing_key_raises():
    with pytest.raises(ValueError, match="DEEPGRAM_API_KEY"):
        DeepgramRestSTTService(api_key="")


# --- Cartesia REST TTS ---


def test_tts_turns_text_into_pcm_audio_frame():
    pcm = b"\x01\x02" * 12000  # 0.5 s of fake s16le mono at 24 kHz
    seen = {}

    def handler(request: httpx.Request):
        import json

        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["version"] = request.headers["Cartesia-Version"]
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, content=pcm)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tts = CartesiaRestTTSService(
        api_key="test-cartesia-key", voice_id="voice-austin", http_client=client
    )
    frames = _run(_collect(tts.run_tts("How much does it cost?", "ctx-1")))

    assert seen["url"] == CARTESIA_TTS_BYTES_URL
    assert seen["auth"] == "Bearer test-cartesia-key"
    assert seen["version"] == "2025-04-16"
    payload = seen["payload"]
    assert payload["model_id"] == "sonic-3"
    assert payload["transcript"] == "How much does it cost?"
    assert payload["voice"] == {"mode": "id", "id": "voice-austin"}
    assert payload["language"] == "en"
    assert payload["output_format"] == {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": 24000,
    }
    assert len(frames) == 1
    assert isinstance(frames[0], TTSAudioRawFrame)
    assert frames[0].audio == pcm
    assert frames[0].sample_rate == 24000
    assert frames[0].num_channels == 1
    assert frames[0].context_id == "ctx-1"


def test_tts_http_error_yields_error_frame_not_crash():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "bad key"})
        )
    )
    tts = CartesiaRestTTSService(
        api_key="test-cartesia-key", voice_id="voice-austin", http_client=client
    )
    frames = _run(_collect(tts.run_tts("Hello", "ctx-1")))
    assert len(frames) == 1
    assert isinstance(frames[0], ErrorFrame)
    assert "401" in frames[0].error


def test_tts_empty_audio_yields_error_frame():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b""))
    )
    tts = CartesiaRestTTSService(
        api_key="test-cartesia-key", voice_id="voice-austin", http_client=client
    )
    frames = _run(_collect(tts.run_tts("Hello", "ctx-1")))
    assert len(frames) == 1
    assert isinstance(frames[0], ErrorFrame)


def test_tts_missing_key_raises():
    with pytest.raises(ValueError, match="CARTESIA_API_KEY"):
        CartesiaRestTTSService(api_key="", voice_id="voice-austin")


def test_tts_missing_voice_id_raises():
    with pytest.raises(ValueError, match="CARTESIA_VOICE_ID"):
        CartesiaRestTTSService(api_key="test-cartesia-key", voice_id="")


# --- Pipeline wiring ---


def _settings(**overrides):
    base = {
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": "claude-sonnet-5-5",
        "DEEPGRAM_API_KEY": "test-deepgram-key",
        "DEEPGRAM_MODEL": "nova-3",
        "CARTESIA_API_KEY": "test-cartesia-key",
        "CARTESIA_VOICE_ID": "voice-austin",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_live_factories_build_rest_services_without_network():
    # Clients are created lazily on first use, so building the pipeline
    # with keys must not touch the network.
    built = build_pipeline(_settings(), llm=FakeLLM())
    assert isinstance(built.stt, DeepgramRestSTTService)
    assert isinstance(built.tts, CartesiaRestTTSService)
    processor_types = [type(p) for p in built.pipeline.processors]
    assert DeepgramRestSTTService in processor_types
    assert CartesiaRestTTSService in processor_types
