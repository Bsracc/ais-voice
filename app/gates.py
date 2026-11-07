"""Compliance eligibility gates for Phase 2 voice (plan §3.2, Langkah 2).

check_eligibility(session) -> {"allowed": bool, "reasons": [...], "checks": {...}}

Session dict:
  type: "outbound-dial" | "voice-browser"
  phone: str | None          (required for outbound-dial)
  clinic_timezone: str       (IANA name, e.g. "America/New_York")
  clinic_state: str          (e.g. "FL", "TX", "CA")
  recording_enabled: bool    (default False — recording is OFF by default)
  recording_consent: bool    (default False — explicit consent to record)

TWO SESSION TYPES — the distinction matters:
  * outbound-dial is a real telephone call. ALL gates apply: kill
    switch, recording, written consent, Phase 1 opt-out, DNC scrub,
    local calling hours, and the dial cap. One failure -> no dial.
  * voice-browser is a voice test in the browser (Langkah 1). It is
    NOT a telephone call, so consent / DNC / hours / cap do NOT apply
    (there is no number being called). Only the kill switch and the
    recording-off gate are checked. A browser test must never be
    blocked because no clinic consented to a call that is not happening.

Gates (outbound-dial), each with its own reason code:
  kill_switch_engaged      kill switch file says engaged
  recording_without_consent recording on without explicit recording consent
  missing_phone            no phone number on an outbound-dial session
  invalid_timezone         clinic_timezone is not a valid IANA name
  no_consent               phone not in data/consent.json (written consent)
  opted_out                 phone matches a Phase 1 opt-out (via HTTP)
  phase1_unreachable       Phase 1 could not be reached -> FAIL CLOSED
  dnc_national / dnc_state phone is on a loaded DNC scrub list
  dnc_scrub_stale          a loaded DNC scrub file is older than 31 days
  outside_calling_hours    local clinic time outside 08:00-21:00
                           (Florida: 08:00-20:00)
  dial_cap_exceeded        already 3 dials to this phone in the last 24h

DNC note: only REAL scrub files (data/dnc_national.json,
data/dnc_state.json) are ever loaded. The *.example.json files are
documentation, never loaded. If no scrub file exists the DNC check is
recorded as "n/a" — a real scrub (version <= 31 days old) MUST be
imported before any real dial.

TEST MODE (allow_phase1_unreachable=True): the Twilio trial dialer
runs on Bsr's laptop, where Phase 1 (port 7860) is NOT running — so
its opt-out HTTP check can never succeed there. For a SELF-TEST dial
only (target = Bsr's own verified number, written consent present in
consent.json, TWILIO_TEST_MODE=true) the caller may pass
allow_phase1_unreachable=True: the opt-out check is then recorded as
"n/a" and the dial may proceed. Default is False (fail closed), and
the skip is written into the audit entry so it is always visible.
Never use it for a clinic dial.

Every check is appended to data/audit_log.jsonl (phone/session,
time, allowed, reasons) — never any conversation content.

All times are timezone-aware (zoneinfo). `now` is injectable for
tests; naive datetimes are read as UTC.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import killswitch, phase1
from app.phase1 import Phase1Unavailable, normalize_phone

FEDERAL_START_HOUR = 8
FEDERAL_END_HOUR = 21
FLORIDA_END_HOUR = 20
DIAL_CAP_PER_24H = 3
DNC_MAX_AGE_DAYS = 31

__all__ = ["check_eligibility", "normalize_phone"]


def _data_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _load_json(path: Path | None, default):
    if path is None:
        return default
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _as_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def _check_dnc(
    path: Path | None, phone: str, now: datetime, reason: str
) -> tuple[bool | str, str | None]:
    """Returns (passed, reason). passed is "n/a" when no scrub file exists."""
    if path is None or not Path(path).exists():
        return "n/a", None
    data = _load_json(Path(path), None)
    if not isinstance(data, dict):
        return False, "dnc_scrub_stale"
    version = data.get("version")
    try:
        version_date = date.fromisoformat(str(version))
    except (ValueError, TypeError):
        return False, "dnc_scrub_stale"
    if (now.date() - version_date).days > DNC_MAX_AGE_DAYS:
        return False, "dnc_scrub_stale"
    wanted = normalize_phone(phone)
    numbers = data.get("numbers", [])
    if any(normalize_phone(str(n)) == wanted for n in numbers):
        return False, reason
    return True, None


def _count_dials_24h(log: list, phone: str, now: datetime) -> int:
    wanted = normalize_phone(phone)
    window_start = now - timedelta(hours=24)
    count = 0
    for entry in log:
        if not isinstance(entry, dict):
            continue
        if normalize_phone(str(entry.get("phone", ""))) != wanted:
            continue
        raw = str(entry.get("dialed_at", ""))
        try:
            dialed_at = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if dialed_at.tzinfo is None:
            dialed_at = dialed_at.replace(tzinfo=UTC)
        if window_start <= dialed_at <= now:
            count += 1
    return count


def _audit(audit_path: Path | None, entry: dict) -> None:
    """Best-effort: a broken audit log must never break the gate."""
    try:
        path = Path(audit_path) if audit_path is not None else _data_dir() / "audit_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def check_eligibility(
    session: dict,
    now: datetime | None = None,
    *,
    consent_path: Path | str | None = None,
    dial_log_path: Path | str | None = None,
    kill_switch_path: Path | str | None = None,
    dnc_national_path: Path | str | None = None,
    dnc_state_path: Path | str | None = None,
    audit_path: Path | str | None = None,
    allow_phase1_unreachable: bool = False,
) -> dict:
    """Run every applicable compliance gate. Returns allowed/reasons/checks.

    allow_phase1_unreachable: TEST ONLY (see module docstring). When
    True and Phase 1 cannot be reached, the opt-out check is recorded
    as "n/a" instead of failing the dial. Default False: fail closed.
    """
    now = _as_now(now)
    data_dir = _data_dir()
    session_type = session.get("type", "outbound-dial")
    phone = session.get("phone")
    reasons: list[str] = []
    notes: list[str] = []
    checks: dict[str, bool | str] = {}

    # --- gates that apply to EVERY session type --------------------------
    checks["kill_switch"] = not killswitch.is_engaged(
        kill_switch_path if kill_switch_path is not None else data_dir / "kill_switch.json"
    )
    if not checks["kill_switch"]:
        reasons.append("kill_switch_engaged")

    recording_on = bool(session.get("recording_enabled", False))
    recording_ok = (not recording_on) or bool(session.get("recording_consent", False))
    checks["recording"] = recording_ok
    if not recording_ok:
        reasons.append("recording_without_consent")

    # --- voice-browser: not a phone call, the rest does not apply --------
    if session_type == "voice-browser":
        for key in ("consent", "opt_out", "dnc", "calling_hours", "dial_cap"):
            checks[key] = "n/a"
        result = {"allowed": not reasons, "reasons": reasons, "checks": checks}
        _audit(
            audit_path,
            {
                "ts": now.isoformat(),
                "type": session_type,
                "phone": phone,
                "allowed": result["allowed"],
                "reasons": reasons,
            },
        )
        return result

    # --- outbound-dial: every remaining gate applies ---------------------
    if not phone:
        checks["consent"] = checks["opt_out"] = checks["dnc"] = False
        checks["calling_hours"] = checks["dial_cap"] = "n/a"
        reasons.append("missing_phone")
        result = {"allowed": False, "reasons": reasons, "checks": checks}
        _audit(
            audit_path,
            {
                "ts": now.isoformat(),
                "type": session_type,
                "phone": None,
                "allowed": False,
                "reasons": reasons,
            },
        )
        return result

    # Written consent (local list; empty file -> nobody passes).
    consent = _load_json(
        Path(consent_path) if consent_path is not None else data_dir / "consent.json", []
    )
    wanted = normalize_phone(str(phone))
    checks["consent"] = any(
        isinstance(row, dict) and normalize_phone(str(row.get("phone", ""))) == wanted
        for row in (consent if isinstance(consent, list) else [])
    )
    if not checks["consent"]:
        reasons.append("no_consent")

    # Phase 1 opt-out (HTTP, read-only). Unreachable -> fail closed,
    # unless the caller explicitly opted into test mode (see docstring).
    try:
        opted_out = phase1.is_opted_out(str(phone))
    except Phase1Unavailable:
        if allow_phase1_unreachable:
            checks["opt_out"] = "n/a"
            notes.append(
                "phase1 opt-out check skipped: Phase 1 unreachable (test mode)"
            )
        else:
            checks["opt_out"] = False
            reasons.append("phase1_unreachable")
    else:
        checks["opt_out"] = not opted_out
        if opted_out:
            reasons.append("opted_out")

    # DNC scrub (real files only; absent -> "n/a", stale -> fail closed).
    dnc_ok: bool | str = True
    for path, reason in (
        (
            dnc_national_path if dnc_national_path is not None else data_dir / "dnc_national.json",
            "dnc_national",
        ),
        (
            dnc_state_path if dnc_state_path is not None else data_dir / "dnc_state.json",
            "dnc_state",
        ),
    ):
        passed, dnc_reason = _check_dnc(Path(path), str(phone), now, reason)
        if passed == "n/a":
            dnc_ok = "n/a" if dnc_ok is True else dnc_ok
        elif not passed:
            dnc_ok = False
            reasons.append(dnc_reason or reason)
    checks["dnc"] = dnc_ok

    # Calling hours in the CLINIC's local time (never WIB).
    tz_name = session.get("clinic_timezone") or ""
    try:
        clinic_tz = ZoneInfo(str(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        checks["calling_hours"] = False
        reasons.append("invalid_timezone")
        clinic_tz = None
    if clinic_tz is not None:
        local = now.astimezone(clinic_tz)
        end_hour = (
            FLORIDA_END_HOUR
            if str(session.get("clinic_state", "")).strip().upper() == "FL"
            else FEDERAL_END_HOUR
        )
        in_hours = FEDERAL_START_HOUR <= local.hour < end_hour
        checks["calling_hours"] = in_hours
        if not in_hours:
            reasons.append("outside_calling_hours")

    # Dial cap: max 3 dials per phone per 24 hours (Florida rule).
    log = _load_json(
        Path(dial_log_path) if dial_log_path is not None else data_dir / "dial_log.json", []
    )
    dials = _count_dials_24h(log if isinstance(log, list) else [], str(phone), now)
    checks["dial_cap"] = dials < DIAL_CAP_PER_24H
    if not checks["dial_cap"]:
        reasons.append("dial_cap_exceeded")

    result = {"allowed": not reasons, "reasons": reasons, "checks": checks}
    _audit(
        audit_path,
        {
            "ts": now.isoformat(),
            "type": session_type,
            "phone": str(phone),
            "allowed": result["allowed"],
            "reasons": reasons,
            "notes": notes,
        },
    )
    return result
