"""Settings for the AISIMA voice service (Phase 2).

Separate service from Phase 1 (ais-autonomous-sales): this service pins
anthropic 1.x for Pipecat 1.15, while Phase 1 stays on anthropic 2.x.
All API keys default to empty — a live pipeline refuses to start without
them (see app/pipeline.py). Recording is OFF by default: California and
Florida are all-party consent states, so recording needs an explicit
announcement + consent before it may be enabled.
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Brain mode: "anthropic" answers with Claude (live LLM), "muse-operator"
    # queues each user turn for a human operator (see app/operator.py) and
    # builds the pipeline without any LLM processor. Default "anthropic"
    # keeps the existing live behaviour unchanged.
    BRAIN_MODE: str = "anthropic"

    @field_validator("BRAIN_MODE", mode="before")
    @classmethod
    def _normalize_brain_mode(cls, value):
        normalized = value.strip().lower() if isinstance(value, str) else value
        if normalized not in ("anthropic", "muse-operator"):
            raise ValueError(
                f"invalid BRAIN_MODE: {value!r}. "
                "Allowed values: 'anthropic', 'muse-operator'."
            )
        return normalized

    # Claude (LLM) — Pipecat 1.15 requires anthropic SDK 1.x.
    # ANTHROPIC_BASE_URL: empty = api.anthropic.com (official). Set it to
    # an Anthropic-compatible gateway (e.g. Nagoya Router) to run the
    # brain through that gateway instead: the same key + model fields
    # are used, only the endpoint changes. For Anthropic-protocol
    # gateways the base URL has NO trailing /v1 (the SDK appends
    # /v1/messages itself).
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-5-5"

    # Deepgram (STT)
    DEEPGRAM_API_KEY: str = ""
    DEEPGRAM_MODEL: str = "nova-3"

    # Cartesia (TTS, Sonic 3 family). Voice ID is empty until Bsr picks a
    # voice in the Cartesia dashboard — no default voice is invented here.
    CARTESIA_API_KEY: str = ""
    CARTESIA_VOICE_ID: str = ""

    # Twilio trial dial-out (test calls only — see DIAL_TEST.md).
    # All five are empty until Bsr signs up for the free Twilio trial
    # and fills them in: the dialer refuses to run without them, and no
    # number or key is ever hard-coded. TWILIO_STREAM_URL is the public
    # wss:// URL of this service's /twilio/stream endpoint (via ngrok
    # on the laptop); TWILIO_TEST_MODE=true means "self-test only"
    # (Phase 1 is not running on the laptop, so its opt-out check is
    # recorded as n/a instead of failing the dial — see gates.py).
    # For real clinic dials TEST_MODE must be false.
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""
    TWILIO_TO_NUMBER: str = ""
    TWILIO_STREAM_URL: str = ""
    TWILIO_TEST_MODE: bool = False
    TWILIO_TEST_TIMEZONE: str = "Asia/Jakarta"
    TWILIO_TEST_STATE: str = ""

    # Offer — dental clinic website redesign (Himmah practice).
    AGENCY_NAME: str = "AISIMA"
    SERVICE_NAME: str = "Website Redesign for Dental Clinics"
    SERVICE_PRICE: str = "$1,000 one-time"
    TARGET_MARKET: str = "USA — Texas, Florida, California dental clinics"

    # Recording is OFF by default. Never enable for CA/FL calls without
    # an announcement + explicit consent (see PHASE2_PLAN.md §3.2 item 7).
    RECORDING_ENABLED: bool = False

    # Phase 1 dashboard (text service) — consulted over HTTP for DNC /
    # opt-out / calling-hours checks. Never write to its DB directly.
    PHASE1_BASE_URL: str = "http://127.0.0.1:7860"

    # Local bind for this voice service.
    HOST: str = "127.0.0.1"
    PORT: int = 7861

    @property
    def ai_disclosure(self) -> str:
        """First-sentence AI disclosure, required on every call."""
        return f"I am an AI assistant calling on behalf of {self.AGENCY_NAME}."
