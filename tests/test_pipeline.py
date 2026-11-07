"""Pipeline construction: fakes build offline, live factories require keys."""

import pytest
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker

from app.config import Settings
from app.fakes import FakeLLM, FakeSTT, FakeTransport, FakeTTS
from app.pipeline import build_pipeline, start_greeting_text


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


def test_build_with_fakes_needs_no_keys_and_no_network():
    settings = _settings()
    built = build_pipeline(settings, stt=FakeSTT(), tts=FakeTTS(), llm=FakeLLM())
    assert isinstance(built.pipeline, Pipeline)
    assert isinstance(built.worker, PipelineWorker)
    assert isinstance(built.stt, FakeSTT)
    assert isinstance(built.tts, FakeTTS)
    assert isinstance(built.llm, FakeLLM)
    # The fakes are actually wired into the pipeline chain.
    processor_types = [type(p) for p in built.pipeline.processors]
    assert FakeSTT in processor_types
    assert FakeTTS in processor_types
    assert FakeLLM in processor_types


def test_build_with_fake_transport_wires_input_output():
    settings = _settings()
    transport = FakeTransport()
    built = build_pipeline(
        settings, stt=FakeSTT(), tts=FakeTTS(), llm=FakeLLM(), transport=transport
    )
    assert built.transport is transport
    processor_types = [type(p) for p in built.pipeline.processors]
    assert FakeSTT in processor_types
    assert FakeLLM in processor_types


def test_live_llm_without_key_raises():
    settings = _settings()
    with pytest.raises(ValueError, match="missing API key"):
        build_pipeline(settings, stt=FakeSTT(), tts=FakeTTS(), llm=None)


def test_live_stt_without_key_raises():
    settings = _settings()
    with pytest.raises(ValueError, match="missing API key"):
        build_pipeline(settings, stt=None, tts=FakeTTS(), llm=FakeLLM())


def test_live_tts_without_key_raises():
    settings = _settings()
    with pytest.raises(ValueError, match="missing API key"):
        build_pipeline(settings, stt=FakeSTT(), tts=None, llm=FakeLLM())


def test_live_tts_without_voice_id_raises():
    settings = _settings(CARTESIA_API_KEY="test-cartesia-key")
    with pytest.raises(ValueError, match="CARTESIA_VOICE_ID"):
        build_pipeline(settings, stt=FakeSTT(), tts=None, llm=FakeLLM())


def test_greeting_opens_with_ai_disclosure():
    text = start_greeting_text(_settings())
    assert text.startswith("Hi, I am an AI assistant calling on behalf of AISIMA")
    assert "AISIMA" in text


def test_operator_mode_builds_without_anthropic_key_and_no_llm():
    settings = _settings(BRAIN_MODE="muse-operator")
    built = build_pipeline(settings, stt=FakeSTT(), tts=FakeTTS(), llm=None)
    assert built.llm is None
    assert isinstance(built.stt, FakeSTT)
    assert isinstance(built.tts, FakeTTS)
    processor_types = [type(p) for p in built.pipeline.processors]
    assert FakeSTT in processor_types
    assert FakeTTS in processor_types
    assert FakeLLM not in processor_types


def test_operator_mode_with_fake_llm_uses_it():
    settings = _settings(BRAIN_MODE="muse-operator")
    built = build_pipeline(settings, stt=FakeSTT(), tts=FakeTTS(), llm=FakeLLM())
    assert isinstance(built.llm, FakeLLM)
    processor_types = [type(p) for p in built.pipeline.processors]
    assert FakeLLM in processor_types


def test_operator_mode_with_fake_transport_wires_input_output():
    settings = _settings(BRAIN_MODE="muse-operator")
    transport = FakeTransport()
    built = build_pipeline(settings, stt=FakeSTT(), tts=FakeTTS(), transport=transport)
    assert built.transport is transport
    assert built.llm is None


def test_anthropic_mode_without_key_still_raises():
    settings = _settings(BRAIN_MODE="anthropic")
    with pytest.raises(ValueError, match="missing API key"):
        build_pipeline(settings, stt=FakeSTT(), tts=FakeTTS(), llm=None)


def test_invalid_brain_mode_raises():
    # Bypass Settings validation to prove build_pipeline itself rejects it.
    settings = Settings.model_construct(BRAIN_MODE="bogus")
    with pytest.raises(ValueError, match="BRAIN_MODE"):
        build_pipeline(settings, stt=FakeSTT(), tts=FakeTTS(), llm=FakeLLM())
