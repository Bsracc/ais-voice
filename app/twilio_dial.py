"""Twilio trial test dialer for the AIS voice service (Phase 2).

Places ONE outbound call through the Twilio Calls REST API — no
Twilio Python SDK needed (plain httpx, already a dependency). The
call's audio is then streamed back to this service over the public
WebSocket (app/twilio_transport.py + POST /twilio/voice + WS
/twilio/stream in app/server.py).

Order of operations in request_dial(), enforced in code:
  1. Twilio settings present (SID, token, from/to numbers, stream URL).
  2. Compliance gates (app/gates.check_eligibility, type outbound-dial):
     kill switch, written consent for the target number, Phase 1
     opt-out, DNC, local calling hours, 3-dial/24h cap. One failure ->
     NO call is placed and nothing is logged as a dial.
  3. Twilio Calls API: POST /Calls.json (To / From / Url=TwiML URL).
  4. The successful dial is appended to data/dial_log.json (the cap
     counts ACTUAL dials only) and the gate audit entry is written by
     check_eligibility itself.

TEST ONLY: this dialer is built for the Twilio trial, whose credit
only reaches VERIFIED numbers — i.e. Bsr's own phone. It never dials
a clinic. TWILIO_TEST_MODE=true (default for the test) records the
Phase 1 opt-out check as "n/a" when Phase 1 is not running (the
laptop does not run Phase 1); for any real clinic dial TEST_MODE
must be false and Phase 1 must be reachable, or the gate fails
closed. No phone number or credential is ever hard-coded here — all
of it comes from the environment (ais-voice/.env).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app import gates
from app.config import Settings
from app.twilio_transport import twiml_url_from_stream_url

TWILIO_CALLS_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
HTTP_TIMEOUT_S = 30.0

REQUIRED_TWILIO_KEYS = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "TWILIO_TO_NUMBER",
    "TWILIO_STREAM_URL",
)


def _data_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _load_json(path: Path | None, default):
    if path is None:
        return default
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _new_http_client() -> httpx.AsyncClient:
    # trust_env=False: the VM's no_proxy contains bracketed IPv6 entries
    # ("[::1]") that httpx cannot parse as URL patterns (same reason as
    # app/rest_services.py). On the laptop there is no proxy env anyway.
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_S,
        trust_env=False,
        verify=os.environ.get("SSL_CERT_FILE") or True,
    )


def missing_twilio_keys(settings: Settings) -> list[str]:
    """Required Twilio settings that are still empty."""
    return [key for key in REQUIRED_TWILIO_KEYS if not getattr(settings, key, "")]


def load_consent_entry(consent_path: Path | str | None, phone: str) -> dict | None:
    """The consent row for this phone, or None.

    The row may carry a "timezone" field (e.g. "Asia/Jakarta" for the
    self-test): the calling-hours gate is then judged in the
    recipient's local time. Without it the dialer falls back to
    TWILIO_TEST_TIMEZONE.
    """
    from app.phase1 import normalize_phone

    wanted = normalize_phone(str(phone))
    rows = _load_json(
        Path(consent_path) if consent_path is not None else _data_dir() / "consent.json",
        [],
    )
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and normalize_phone(str(row.get("phone", ""))) == wanted:
            return row
    return None


def build_dial_session(settings: Settings, consent_entry: dict | None = None) -> dict:
    """The outbound-dial session dict for gates.check_eligibility."""
    entry = consent_entry or {}
    return {
        "type": "outbound-dial",
        "phone": settings.TWILIO_TO_NUMBER,
        "clinic_timezone": str(entry.get("timezone") or settings.TWILIO_TEST_TIMEZONE or ""),
        "clinic_state": str(settings.TWILIO_TEST_STATE or ""),
        "recording_enabled": False,
        "recording_consent": False,
    }


async def place_call(
    *,
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_number: str,
    twiml_url: str,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """POST one call to the Twilio Calls API. Returns the parsed call
    resource (sid, status, ...). Raises ValueError when Twilio refuses
    (bad credentials, unverified trial number, bad caller ID, ...)."""
    owned = http_client is None
    client = http_client if http_client is not None else _new_http_client()
    try:
        response = await client.post(
            TWILIO_CALLS_URL.format(sid=account_sid),
            auth=httpx.BasicAuth(account_sid, auth_token),
            data={"To": to_number, "From": from_number, "Url": twiml_url},
        )
    finally:
        if owned:
            await client.aclose()
    if response.status_code != 201:
        detail = response.text[:300]
        raise ValueError(
            f"Twilio call failed (HTTP {response.status_code}): {detail}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ValueError("Twilio returned an unreadable call resource") from exc


def record_dial(
    dial_log_path: Path | str | None,
    phone: str,
    call_sid: str,
    now: datetime | None = None,
) -> None:
    """Append one successful dial to dial_log.json (the cap's source)."""
    path = Path(dial_log_path) if dial_log_path is not None else _data_dir() / "dial_log.json"
    log = _load_json(path, [])
    if not isinstance(log, list):
        log = []
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    log.append(
        {
            "phone": phone,
            "dialed_at": moment.isoformat(),
            "call_sid": call_sid,
            "provider": "twilio-trial",
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2))


async def request_dial(
    settings: Settings,
    now: datetime | None = None,
    *,
    consent_path: Path | str | None = None,
    dial_log_path: Path | str | None = None,
    kill_switch_path: Path | str | None = None,
    dnc_national_path: Path | str | None = None,
    dnc_state_path: Path | str | None = None,
    audit_path: Path | str | None = None,
    http_client: httpx.AsyncClient | None = None,
    dry_run: bool = False,
) -> dict:
    """Gate, then dial. Returns {"allowed", "reasons", "checks", "call"}.

    "call" is None unless a call was actually placed. A refused gate
    never touches the Twilio API and never writes the dial log.
    dry_run=True runs the gates only: when they pass it returns
    allowed=True with "dry_run": True and places no call.
    """
    missing = missing_twilio_keys(settings)
    if missing:
        return {
            "allowed": False,
            "reasons": ["twilio_not_configured"],
            "checks": {"twilio_configured": False},
            "missing": missing,
            "call": None,
        }

    data_dir = _data_dir()
    entry = load_consent_entry(
        consent_path if consent_path is not None else data_dir / "consent.json",
        settings.TWILIO_TO_NUMBER,
    )
    session = build_dial_session(settings, entry)
    gate = gates.check_eligibility(
        session,
        now=now,
        consent_path=consent_path if consent_path is not None else data_dir / "consent.json",
        dial_log_path=dial_log_path if dial_log_path is not None else data_dir / "dial_log.json",
        kill_switch_path=kill_switch_path
        if kill_switch_path is not None
        else data_dir / "kill_switch.json",
        dnc_national_path=dnc_national_path
        if dnc_national_path is not None
        else data_dir / "dnc_national.json",
        dnc_state_path=dnc_state_path
        if dnc_state_path is not None
        else data_dir / "dnc_state.json",
        audit_path=audit_path if audit_path is not None else data_dir / "audit_log.jsonl",
        allow_phase1_unreachable=bool(settings.TWILIO_TEST_MODE),
    )
    if not gate["allowed"]:
        return {
            "allowed": False,
            "reasons": gate["reasons"],
            "checks": gate["checks"],
            "call": None,
        }

    if dry_run:
        return {
            "allowed": True,
            "reasons": [],
            "checks": gate["checks"],
            "call": None,
            "dry_run": True,
        }

    twiml_url = twiml_url_from_stream_url(settings.TWILIO_STREAM_URL)
    call = await place_call(
        account_sid=settings.TWILIO_ACCOUNT_SID,
        auth_token=settings.TWILIO_AUTH_TOKEN,
        from_number=settings.TWILIO_FROM_NUMBER,
        to_number=settings.TWILIO_TO_NUMBER,
        twiml_url=twiml_url,
        http_client=http_client,
    )
    record_dial(
        dial_log_path if dial_log_path is not None else data_dir / "dial_log.json",
        settings.TWILIO_TO_NUMBER,
        str(call.get("sid", "")),
        now=now,
    )
    return {
        "allowed": True,
        "reasons": [],
        "checks": gate["checks"],
        "call": {"sid": call.get("sid"), "status": call.get("status")},
    }


__all__ = [
    "REQUIRED_TWILIO_KEYS",
    "TWILIO_CALLS_URL",
    "build_dial_session",
    "load_consent_entry",
    "missing_twilio_keys",
    "place_call",
    "record_dial",
    "request_dial",
]
