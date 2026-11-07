#!/usr/bin/env python3
"""Twilio trial test dialer CLI for the AIS voice service.

Places ONE test call to Bsr's own verified phone number (Twilio trial
credit only reaches verified numbers). Run from the ais-voice repo
root, with the voice server already running on the laptop:

    uv run python scripts/twilio_dial.py --dry-run
        Run the compliance gates only (consent, hours, cap, kill
        switch). Places NO call. Use this first.

    uv run python scripts/twilio_dial.py
        Gate, then place the call. Your phone rings; answer it and the
        AI speaks its disclosure first. The transcript lands in
        data/transcript.jsonl on the laptop.

All settings come from ais-voice/.env (TWILIO_ACCOUNT_SID,
TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER,
TWILIO_STREAM_URL, TWILIO_TEST_MODE). Nothing is dialed when a gate
refuses, and the dial is recorded in data/dial_log.json (cap: 3 per
24h per number). Exit code 0 = call placed (or gates pass on
--dry-run), 1 = refused by a gate, 2 = not configured / Twilio error.

See DIAL_TEST.md for the full test procedure.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.twilio_dial import missing_twilio_keys, request_dial


async def _main(dry_run: bool) -> int:
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 — a broken .env must print, not traceback.
        print(f"settings error: cannot read .env: {exc}")
        return 2
    missing = missing_twilio_keys(settings)
    if missing:
        print("Twilio not configured — missing in ais-voice/.env: " + ", ".join(missing))
        print("See DIAL_TEST.md step (b).")
        return 2
    try:
        result = await request_dial(settings, dry_run=dry_run)
    except ValueError as exc:
        print(f"dial failed: {exc}")
        return 2
    if not result["allowed"]:
        print("REFUSED — no call placed. Reasons:")
        for reason in result["reasons"]:
            print(f"  - {reason}")
        print("Checks:", result["checks"])
        return 1
    if result.get("dry_run"):
        print("GATES PASS — no call placed (--dry-run).")
        print("Checks:", result["checks"])
        return 0
    call = result["call"] or {}
    print(f"CALL PLACED — sid={call.get('sid')} status={call.get('status')}")
    print(f"Calling {settings.TWILIO_TO_NUMBER} from {settings.TWILIO_FROM_NUMBER}.")
    print("Answer your phone: the AI speaks its disclosure first.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the compliance gates only; place no call",
    )
    args = parser.parse_args()
    return asyncio.run(_main(args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
