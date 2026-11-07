"""Tests for the Twilio trial dialer (app/twilio_dial.py).

Gate-before-dial is the whole point: a refused gate must never touch
the Twilio API and never write the dial log. All Twilio HTTP is faked
with httpx.MockTransport — no keys, no network.
"""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from app import gates
from app.config import Settings
from app.phase1 import Phase1Unavailable
from app.twilio_dial import (
    build_dial_session,
    load_consent_entry,
    place_call,
    record_dial,
    request_dial,
)

# 2026-11-07 14:00 UTC == 09:00 EST (America/New_York) — inside hours.
IN_HOURS = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)
# 2026-11-08 03:00 UTC == 22:00 EST 7 Nov — outside federal hours.
OUT_OF_HOURS = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)

PHONE = "+1 (555) 010-0100"  # fictional 555 test number, never a real number
CONSENT_ENTRY = {
    "phone": "+1-555-010-0100",
    "name": "Test Clinic (fictional)",
    "consented_at": "2026-11-06T10:00:00Z",
    "channel": "email",
    "seller": "AISIMA",
    "timezone": "America/New_York",
}


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "consent.json").write_text(json.dumps([CONSENT_ENTRY]))
    (d / "dial_log.json").write_text("[]")
    (d / "kill_switch.json").write_text(json.dumps({"engaged": False}))
    (d / "audit_log.jsonl").write_text("")
    return d


@pytest.fixture
def paths(data_dir):
    return {
        "consent_path": data_dir / "consent.json",
        "dial_log_path": data_dir / "dial_log.json",
        "kill_switch_path": data_dir / "kill_switch.json",
        "dnc_national_path": data_dir / "dnc_national.json",
        "dnc_state_path": data_dir / "dnc_state.json",
        "audit_path": data_dir / "audit_log.jsonl",
    }


def _settings(**overrides):
    base = {
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": "claude-sonnet-5-5",
        "DEEPGRAM_API_KEY": "",
        "CARTESIA_API_KEY": "",
        "CARTESIA_VOICE_ID": "",
        "TWILIO_ACCOUNT_SID": "ACtest123",
        "TWILIO_AUTH_TOKEN": "test-token",
        "TWILIO_FROM_NUMBER": "+15550001111",
        "TWILIO_TO_NUMBER": PHONE,
        "TWILIO_STREAM_URL": "wss://abc123.ngrok-free.app/twilio/stream",
        "TWILIO_TEST_MODE": True,
        "TWILIO_TEST_TIMEZONE": "Asia/Jakarta",
        "TWILIO_TEST_STATE": "",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class _TwilioApiFake:
    """httpx.MockTransport handler recording Twilio Calls API hits."""

    def __init__(self, status=201, payload=None):
        self.calls = []
        self.status = status
        self.payload = payload or {"sid": "CA123", "status": "queued"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(self.status, json=self.payload)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def phase1_down(monkeypatch):
    def _boom(phone):
        raise Phase1Unavailable("connection refused")

    monkeypatch.setattr(gates.phase1, "is_opted_out", _boom)


@pytest.fixture
def no_opt_out(monkeypatch):
    monkeypatch.setattr(gates.phase1, "is_opted_out", lambda phone: False)


def _dial_log(data_dir):
    return json.loads((data_dir / "dial_log.json").read_text())


# --- consent / session building -----------------------------------------


def test_load_consent_entry_matches_normalized_phone(data_dir):
    entry = load_consent_entry(data_dir / "consent.json", PHONE)
    assert entry is not None
    assert entry["timezone"] == "America/New_York"
    assert load_consent_entry(data_dir / "consent.json", "+1-555-999-9999") is None


def test_build_dial_session_uses_consent_timezone():
    session = build_dial_session(_settings(), CONSENT_ENTRY)
    assert session["type"] == "outbound-dial"
    assert session["phone"] == PHONE
    assert session["clinic_timezone"] == "America/New_York"
    assert session["recording_enabled"] is False


def test_build_dial_session_falls_back_to_test_timezone():
    session = build_dial_session(_settings(), None)
    assert session["clinic_timezone"] == "Asia/Jakarta"


# --- place_call ------------------------------------------------------------


def test_place_call_posts_to_twilio_calls_api():
    fake = _TwilioApiFake()
    call = asyncio.run(
        place_call(
            account_sid="ACtest123",
            auth_token="test-token",
            from_number="+15550001111",
            to_number=PHONE,
            twiml_url="https://abc123.ngrok-free.app/twilio/voice",
            http_client=fake.client(),
        )
    )
    assert call["sid"] == "CA123"
    assert len(fake.calls) == 1
    request = fake.calls[0]
    assert request.method == "POST"
    assert "ACtest123" in str(request.url)
    assert "Calls.json" in str(request.url)
    body = request.read().decode()
    assert "To=%2B1+%28555%29+010-0100" in body or "To=" in body
    assert "From=" in body and "Url=" in body


def test_place_call_twilio_error_raises():
    fake = _TwilioApiFake(
        status=400,
        payload={"code": 21211, "message": "The 'To' number is not verified."},
    )
    with pytest.raises(ValueError, match="Twilio call failed.*400"):
        asyncio.run(
            place_call(
                account_sid="ACtest123",
                auth_token="test-token",
                from_number="+15550001111",
                to_number=PHONE,
                twiml_url="https://abc123.ngrok-free.app/twilio/voice",
                http_client=fake.client(),
            )
        )


# --- request_dial: gates before any call -----------------------------------


def test_request_dial_no_consent_refused_no_http(paths, data_dir, no_opt_out):
    (paths["consent_path"]).write_text("[]")
    fake = _TwilioApiFake()
    result = asyncio.run(
        request_dial(_settings(), now=IN_HOURS, http_client=fake.client(), **paths)
    )
    assert result["allowed"] is False
    assert "no_consent" in result["reasons"]
    assert result["call"] is None
    assert fake.calls == []
    assert _dial_log(data_dir) == []


def test_request_dial_outside_hours_refused_no_http(paths, no_opt_out, data_dir):
    fake = _TwilioApiFake()
    result = asyncio.run(
        request_dial(_settings(), now=OUT_OF_HOURS, http_client=fake.client(), **paths)
    )
    assert result["allowed"] is False
    assert "outside_calling_hours" in result["reasons"]
    assert fake.calls == []
    assert _dial_log(data_dir) == []


def test_request_dial_cap_refused_no_http(paths, no_opt_out, data_dir):
    log = [
        {"phone": PHONE, "dialed_at": "2026-11-07T13:00:00+00:00"},
        {"phone": PHONE, "dialed_at": "2026-11-07T10:00:00+00:00"},
        {"phone": PHONE, "dialed_at": "2026-11-06T15:00:00+00:00"},
    ]
    (paths["dial_log_path"]).write_text(json.dumps(log))
    fake = _TwilioApiFake()
    result = asyncio.run(
        request_dial(_settings(), now=IN_HOURS, http_client=fake.client(), **paths)
    )
    assert result["allowed"] is False
    assert "dial_cap_exceeded" in result["reasons"]
    assert fake.calls == []
    assert len(_dial_log(data_dir)) == 3


def test_request_dial_kill_switch_refused(paths, data_dir, no_opt_out):
    (paths["kill_switch_path"]).write_text(json.dumps({"engaged": True}))
    fake = _TwilioApiFake()
    result = asyncio.run(
        request_dial(_settings(), now=IN_HOURS, http_client=fake.client(), **paths)
    )
    assert result["allowed"] is False
    assert "kill_switch_engaged" in result["reasons"]
    assert fake.calls == []


def test_request_dial_missing_config_no_http(paths, data_dir):
    settings = _settings(TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="")
    fake = _TwilioApiFake()
    result = asyncio.run(
        request_dial(settings, now=IN_HOURS, http_client=fake.client(), **paths)
    )
    assert result["allowed"] is False
    assert "twilio_not_configured" in result["reasons"]
    assert set(result["missing"]) >= {"TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"}
    assert fake.calls == []


def test_request_dial_phase1_down_fails_closed_when_not_test_mode(
    paths, data_dir, phase1_down
):
    settings = _settings(TWILIO_TEST_MODE=False)
    fake = _TwilioApiFake()
    result = asyncio.run(
        request_dial(settings, now=IN_HOURS, http_client=fake.client(), **paths)
    )
    assert result["allowed"] is False
    assert "phase1_unreachable" in result["reasons"]
    assert fake.calls == []


def test_request_dial_success_places_call_and_logs_it(paths, data_dir, phase1_down):
    fake = _TwilioApiFake()
    result = asyncio.run(
        request_dial(_settings(), now=IN_HOURS, http_client=fake.client(), **paths)
    )
    assert result["allowed"] is True
    assert result["reasons"] == []
    assert result["call"] == {"sid": "CA123", "status": "queued"}
    assert len(fake.calls) == 1
    log = _dial_log(data_dir)
    assert len(log) == 1
    assert log[0]["phone"] == PHONE
    assert log[0]["call_sid"] == "CA123"
    assert log[0]["provider"] == "twilio-trial"
    audit = (data_dir / "audit_log.jsonl").read_text()
    assert "phase1 opt-out check skipped" in audit


def test_request_dial_dry_run_places_no_call(paths, data_dir, phase1_down):
    fake = _TwilioApiFake()
    result = asyncio.run(
        request_dial(
            _settings(), now=IN_HOURS, http_client=fake.client(), dry_run=True, **paths
        )
    )
    assert result["allowed"] is True
    assert result.get("dry_run") is True
    assert result["call"] is None
    assert fake.calls == []
    assert _dial_log(data_dir) == []


def test_record_dial_appends(tmp_path):
    path = tmp_path / "dial_log.json"
    record_dial(path, PHONE, "CA1", now=IN_HOURS)
    record_dial(path, PHONE, "CA2", now=IN_HOURS)
    log = json.loads(path.read_text())
    assert [row["call_sid"] for row in log] == ["CA1", "CA2"]
    assert log[0]["dialed_at"] == IN_HOURS.isoformat()
