"""Config defaults and env overrides for the AIS voice service."""

import pytest

from app.config import Settings

ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "DEEPGRAM_API_KEY",
    "DEEPGRAM_MODEL",
    "CARTESIA_API_KEY",
    "CARTESIA_VOICE_ID",
    "AGENCY_NAME",
    "SERVICE_NAME",
    "SERVICE_PRICE",
    "TARGET_MARKET",
    "RECORDING_ENABLED",
    "PHASE1_BASE_URL",
    "HOST",
    "PORT",
    "BRAIN_MODE",
]


@pytest.fixture
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_defaults(clean_env):
    settings = Settings(_env_file=None)
    assert settings.ANTHROPIC_API_KEY == ""
    assert settings.ANTHROPIC_MODEL == "claude-sonnet-5-5"
    assert settings.DEEPGRAM_API_KEY == ""
    assert settings.DEEPGRAM_MODEL == "nova-3"
    assert settings.CARTESIA_API_KEY == ""
    assert settings.CARTESIA_VOICE_ID == ""
    assert settings.AGENCY_NAME == "AISIMA"
    assert settings.SERVICE_NAME == "Website Redesign for Dental Clinics"
    assert settings.SERVICE_PRICE == "$1,000 one-time"
    assert settings.TARGET_MARKET == "USA — Texas, Florida, California dental clinics"
    assert settings.PHASE1_BASE_URL == "http://127.0.0.1:7860"
    assert settings.HOST == "127.0.0.1"
    assert settings.PORT == 7861


def test_recording_disabled_by_default(clean_env):
    """Recording must be OFF by default (CA + FL are all-party consent states)."""
    assert Settings(_env_file=None).RECORDING_ENABLED is False


def test_env_override(clean_env, monkeypatch):
    monkeypatch.setenv("RECORDING_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("PORT", "7999")
    settings = Settings(_env_file=None)
    assert settings.RECORDING_ENABLED is True
    assert settings.PORT == 7999


def test_ai_disclosure_mentions_agency(clean_env):
    settings = Settings(_env_file=None)
    assert settings.ai_disclosure == "I am an AI assistant calling on behalf of AISIMA."


def test_brain_mode_defaults_to_anthropic(clean_env):
    assert Settings(_env_file=None).BRAIN_MODE == "anthropic"


def test_brain_mode_muse_operator_accepted(clean_env):
    assert Settings(_env_file=None, BRAIN_MODE="muse-operator").BRAIN_MODE == "muse-operator"


def test_brain_mode_normalizes_case_and_spaces(clean_env):
    assert Settings(_env_file=None, BRAIN_MODE="  Muse-Operator ").BRAIN_MODE == "muse-operator"
    assert Settings(_env_file=None, BRAIN_MODE="ANTHROPIC").BRAIN_MODE == "anthropic"


def test_brain_mode_other_value_rejected(clean_env):
    with pytest.raises(ValueError, match="BRAIN_MODE"):
        Settings(_env_file=None, BRAIN_MODE="sonnet")


def test_brain_mode_env_override(clean_env, monkeypatch):
    monkeypatch.setenv("BRAIN_MODE", "muse-operator")
    assert Settings(_env_file=None).BRAIN_MODE == "muse-operator"
