"""Tests for app.gates — compliance eligibility gates (Phase 2, Langkah 2).

TDD: these tests were written BEFORE app/gates.py existed.
Session types:
  - "outbound-dial": a real phone call. ALL gates apply.
  - "voice-browser": a browser voice test, no phone call. Only the kill
    switch and recording-off gates apply; consent / DNC / hours / cap
    do NOT apply (it is not a telephone call).
"""

import json
from datetime import UTC, datetime

import pytest

from app import gates
from app.phase1 import Phase1Unavailable

# 2026-11-07 14:00 UTC == 09:00 EST (America/New_York, DST ended 1 Nov).
IN_HOURS = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)
# 2026-11-08 03:00 UTC == 22:00 EST 7 Nov — outside federal hours.
OUT_OF_HOURS = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)
# 2026-11-08 01:30 UTC == 20:30 EST 7 Nov — outside Florida hours.
FL_LATE = datetime(2026, 11, 8, 1, 30, tzinfo=UTC)
# 2026-11-08 00:30 UTC == 19:30 EST 7 Nov — inside Florida hours.
FL_OK = datetime(2026, 11, 8, 0, 30, tzinfo=UTC)

PHONE = "+1 (555) 010-0100"  # fictional 555 test number, never a real number
CONSENT_ENTRY = {
    "phone": "+1-555-010-0100",
    "name": "Test Clinic (fictional)",
    "consented_at": "2026-11-06T10:00:00Z",
    "channel": "email",
    "seller": "AISIMA",
}


@pytest.fixture
def data_dir(tmp_path):
    """Fresh data dir: consent has the test number, kill switch off."""
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
        "audit_path": data_dir / "audit_log.jsonl",
        # No real DNC scrub files in tmp dir -> DNC check is skipped.
        "dnc_national_path": data_dir / "dnc_national.json",
        "dnc_state_path": data_dir / "dnc_state.json",
    }


@pytest.fixture
def no_opt_out(monkeypatch):
    monkeypatch.setattr(gates.phase1, "is_opted_out", lambda phone: False)


def dial_session(**overrides):
    s = {
        "type": "outbound-dial",
        "phone": PHONE,
        "clinic_timezone": "America/New_York",
        "clinic_state": "FL",
    }
    s.update(overrides)
    return s


def check(session, paths, now=IN_HOURS):
    return gates.check_eligibility(session, now=now, **paths)


# --- required cases -------------------------------------------------------


def test_no_consent_rejected(paths, no_opt_out):
    """Consent list empty -> dial refused with reason no_consent."""
    (paths["consent_path"]).write_text("[]")
    result = check(dial_session(), paths)
    assert result["allowed"] is False
    assert "no_consent" in result["reasons"]


def test_consented_in_hours_allowed(paths, no_opt_out):
    result = check(dial_session(), paths)
    assert result["allowed"] is True
    assert result["reasons"] == []


def test_voice_browser_needs_no_consent(paths, monkeypatch):
    """Browser voice test: no phone, no consent — still allowed."""
    monkeypatch.setattr(
        gates.phase1,
        "is_opted_out",
        lambda phone: (_ for _ in ()).throw(AssertionError("must not call Phase 1")),
    )
    result = check({"type": "voice-browser"}, paths)
    assert result["allowed"] is True
    assert result["reasons"] == []


def test_phase1_opt_out_rejected(paths, monkeypatch):
    monkeypatch.setattr(gates.phase1, "is_opted_out", lambda phone: True)
    result = check(dial_session(), paths)
    assert result["allowed"] is False
    assert "opted_out" in result["reasons"]


def test_outside_hours_rejected(paths, no_opt_out):
    result = check(dial_session(), paths, now=OUT_OF_HOURS)
    assert result["allowed"] is False
    assert "outside_calling_hours" in result["reasons"]


def test_florida_2030_rejected_1930_allowed(paths, no_opt_out):
    late = check(dial_session(), paths, now=FL_LATE)
    assert late["allowed"] is False
    assert "outside_calling_hours" in late["reasons"]
    ok = check(dial_session(), paths, now=FL_OK)
    assert ok["allowed"] is True


def test_dial_cap_3_per_24h(paths, no_opt_out):
    """3 dials in the last 24h -> the 4th is refused."""
    log = [
        {"phone": PHONE, "dialed_at": "2026-11-07T13:00:00+00:00"},
        {"phone": PHONE, "dialed_at": "2026-11-07T10:00:00+00:00"},
        {"phone": PHONE, "dialed_at": "2026-11-06T15:00:00+00:00"},
    ]
    (paths["dial_log_path"]).write_text(json.dumps(log))
    result = check(dial_session(), paths)
    assert result["allowed"] is False
    assert "dial_cap_exceeded" in result["reasons"]


def test_dial_cap_2_allowed(paths, no_opt_out):
    log = [
        {"phone": PHONE, "dialed_at": "2026-11-07T13:00:00+00:00"},
        {"phone": PHONE, "dialed_at": "2026-11-07T10:00:00+00:00"},
    ]
    (paths["dial_log_path"]).write_text(json.dumps(log))
    assert check(dial_session(), paths)["allowed"] is True


def test_kill_switch_blocks_everything(paths, no_opt_out):
    (paths["kill_switch_path"]).write_text(json.dumps({"engaged": True}))
    assert check(dial_session(), paths)["allowed"] is False
    assert "kill_switch_engaged" in check(dial_session(), paths)["reasons"]
    browser = check({"type": "voice-browser"}, paths)
    assert browser["allowed"] is False
    assert "kill_switch_engaged" in browser["reasons"]


def test_phase1_down_fail_closed_for_dial(paths, monkeypatch):
    """Phase 1 unreachable -> dial refused (fail-closed)."""

    def _boom(phone):
        raise Phase1Unavailable("connection refused")

    monkeypatch.setattr(gates.phase1, "is_opted_out", _boom)
    result = check(dial_session(), paths)
    assert result["allowed"] is False
    assert "phase1_unreachable" in result["reasons"]


def test_phase1_down_does_not_block_browser_test(paths, monkeypatch):
    def _boom(phone):
        raise Phase1Unavailable("connection refused")

    monkeypatch.setattr(gates.phase1, "is_opted_out", _boom)
    assert check({"type": "voice-browser"}, paths)["allowed"] is True


def test_recording_without_consent_rejected(paths, no_opt_out):
    result = check(dial_session(recording_enabled=True), paths)
    assert result["allowed"] is False
    assert "recording_without_consent" in result["reasons"]


def test_recording_with_explicit_consent_allowed(paths, no_opt_out):
    s = dial_session(recording_enabled=True, recording_consent=True)
    assert check(s, paths)["allowed"] is True


def test_recording_off_by_default_passes(paths, no_opt_out):
    """Recording off (default) passes this gate; recording stays off."""
    result = check(dial_session(), paths)
    assert result["checks"]["recording"] is True
    assert result["allowed"] is True


def test_missing_phone_rejected(paths, no_opt_out):
    result = check(dial_session(phone=None), paths)
    assert result["allowed"] is False
    assert "missing_phone" in result["reasons"]


def test_invalid_timezone_fail_closed(paths, no_opt_out):
    result = check(dial_session(clinic_timezone="Not/AZone"), paths)
    assert result["allowed"] is False
    assert "invalid_timezone" in result["reasons"]


# --- DNC scrub (real file only; .example files are never loaded) ----------


def test_dnc_listed_number_rejected(paths, no_opt_out):
    (paths["dnc_national_path"]).write_text(
        json.dumps(
            {
                "version": "2026-11-06",
                "numbers": ["+1-555-010-0100"],
            }
        )
    )
    result = check(dial_session(), paths)
    assert result["allowed"] is False
    assert "dnc_national" in result["reasons"]


def test_stale_dnc_scrub_fail_closed(paths, no_opt_out):
    (paths["dnc_national_path"]).write_text(
        json.dumps(
            {
                "version": "2026-09-01",  # > 31 days before 7 Nov 2026
                "numbers": [],
            }
        )
    )
    result = check(dial_session(), paths)
    assert result["allowed"] is False
    assert "dnc_scrub_stale" in result["reasons"]


# --- audit log ------------------------------------------------------------


def test_audit_log_written_without_conversation_content(paths, no_opt_out):
    check(dial_session(), paths)
    lines = (paths["audit_path"]).read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["allowed"] is True
    assert entry["phone"] == PHONE
    assert entry["type"] == "outbound-dial"
    assert "content" not in entry and "transcript" not in entry


# --- plan §4 Langkah 2: 10 mixed test numbers + 1 allowed -----------------


@pytest.mark.parametrize(
    "label,mutate,now,expected_reason",
    [
        ("no consent", lambda p: p["consent_path"].write_text("[]"), IN_HOURS, "no_consent"),
        ("opted out", None, IN_HOURS, "opted_out"),
        ("outside hours 22:00 local", None, OUT_OF_HOURS, "outside_calling_hours"),
        ("Florida 20:30", None, FL_LATE, "outside_calling_hours"),
        (
            "cap 3x/24h",
            lambda p: p["dial_log_path"].write_text(
                json.dumps(
                    [
                        {"phone": PHONE, "dialed_at": "2026-11-07T13:00:00+00:00"},
                        {"phone": PHONE, "dialed_at": "2026-11-07T10:00:00+00:00"},
                        {"phone": PHONE, "dialed_at": "2026-11-06T15:00:00+00:00"},
                    ]
                )
            ),
            IN_HOURS,
            "dial_cap_exceeded",
        ),
        (
            "kill switch",
            lambda p: p["kill_switch_path"].write_text(json.dumps({"engaged": True})),
            IN_HOURS,
            "kill_switch_engaged",
        ),
        ("phase1 down", None, IN_HOURS, "phase1_unreachable"),
        ("recording no consent", None, IN_HOURS, "recording_without_consent"),
        ("missing phone", None, IN_HOURS, "missing_phone"),
        (
            "DNC listed",
            lambda p: p["dnc_national_path"].write_text(
                json.dumps(
                    {
                        "version": "2026-11-06",
                        "numbers": ["+1-555-010-0100"],
                    }
                )
            ),
            IN_HOURS,
            "dnc_national",
        ),
    ],
)
def test_ten_mixed_numbers_all_rejected(
    label,
    mutate,
    now,
    expected_reason,
    paths,
    monkeypatch,
):
    if mutate is not None:
        mutate(paths)
    if label == "opted out":
        monkeypatch.setattr(gates.phase1, "is_opted_out", lambda phone: True)
    elif label == "phase1 down":

        def _boom(phone):
            raise Phase1Unavailable("down")

        monkeypatch.setattr(gates.phase1, "is_opted_out", _boom)
    else:
        monkeypatch.setattr(gates.phase1, "is_opted_out", lambda phone: False)
    session = dial_session()
    if label == "recording no consent":
        session["recording_enabled"] = True
    if label == "missing phone":
        session["phone"] = None
    result = gates.check_eligibility(session, now=now, **paths)
    assert result["allowed"] is False, label
    assert expected_reason in result["reasons"], label


def test_one_consented_number_in_hours_allowed(paths, no_opt_out):
    result = check(dial_session(), paths)
    assert result["allowed"] is True
    assert result["reasons"] == []
