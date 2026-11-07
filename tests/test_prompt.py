"""Voice prompt: adapted from the Phase 1 text prompt for spoken calls."""

from app.config import Settings
from app.prompt import build_voice_prompt, build_voicemail_script


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


def test_prompt_contains_agency_price_and_disclosure():
    prompt = build_voice_prompt(_settings())
    assert "AISIMA" in prompt
    assert "$1,000" in prompt
    assert "I am an AI assistant calling on behalf of AISIMA" in prompt


def test_prompt_price_comes_from_settings_only():
    prompt = build_voice_prompt(_settings(SERVICE_PRICE="$2,500 one-time"))
    assert "$2,500" in prompt
    assert "$1,000" not in prompt


def test_prompt_voice_rules():
    prompt = build_voice_prompt(_settings())
    lowered = prompt.lower()
    assert "one question" in lowered
    assert "short sentence" in lowered
    assert "opt-out" in lowered or "opt out" in lowered
    assert "voicemail" in lowered


def test_prompt_forbids_invented_claims():
    prompt = build_voice_prompt(_settings())
    lowered = prompt.lower()
    assert "do not invent" in lowered
    assert "testimonial" in lowered
    assert "guarantee" in lowered


def test_voicemail_script_short_and_discloses_ai():
    script = build_voicemail_script(_settings())
    assert "AISIMA" in script
    assert "AI assistant" in script
    # Spoken at ~150 words/minute, 20 seconds ~= 50 words max.
    assert len(script.split()) <= 50
