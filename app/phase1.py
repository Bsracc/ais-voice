"""Read-only HTTP client to the Phase 1 dashboard (port 7860).

Only GET requests, only three endpoints:
  GET /api/health, /api/opt-outs, /api/history/<session_id>

Never writes to the Phase 1 database and never opens data/sales.db
directly. Any failure (connection refused, timeout, 5xx) raises
Phase1Unavailable so callers can fail closed.

NOTE on opt-out matching: Phase 1's opt_outs table is keyed by
session_id, not by phone number. is_opted_out() therefore matches a
phone against the session_id values after normalization. A phone only
matches if the session that opted out was keyed by that phone. The
voice dialer must use the phone number as the Phase 1 session_id when
it records an opt-out, or Phase 1 needs a phone-keyed opt-out
endpoint later. Until then this check is necessary but not sufficient
on its own — consent + DNC + hours + cap gates still apply.
"""

from __future__ import annotations

import os
import re

import httpx

PHASE1_BASE_URL = os.environ.get("PHASE1_BASE_URL", "http://127.0.0.1:7860")
TIMEOUT_SECONDS = 3.0


class Phase1Unavailable(Exception):
    """Phase 1 could not be reached or returned an error."""


def normalize_phone(phone: str) -> str:
    """Digits only; drop a leading US country code 1 (11 -> 10 digits)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _get(path: str, client: httpx.Client | None = None, base_url: str | None = None) -> dict:
    url = (base_url or PHASE1_BASE_URL).rstrip("/") + path
    try:
        if client is not None:
            response = client.get(url, timeout=TIMEOUT_SECONDS)
        else:
            # trust_env=False: this VM's proxy env vars (incl. bracketed
            # IPv6 no_proxy entries) break httpx's proxy parsing, and a
            # localhost client must never go through a proxy anyway.
            with httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False) as owned:
                response = owned.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise Phase1Unavailable(f"Phase 1 request failed ({url}): {exc}") from exc
    return response.json()


def health(client: httpx.Client | None = None, base_url: str | None = None) -> dict:
    """GET /api/health -> e.g. {"status": "ok", "mode": "muse-operator"}."""
    return _get("/api/health", client=client, base_url=base_url)


def get_opt_outs(client: httpx.Client | None = None, base_url: str | None = None) -> list[dict]:
    """GET /api/opt-outs -> list of {"session_id", "created_at"} rows."""
    data = _get("/api/opt-outs", client=client, base_url=base_url)
    return data.get("opt_outs", [])


def get_history(
    session_id: str, client: httpx.Client | None = None, base_url: str | None = None
) -> list[dict]:
    """GET /api/history/<session_id> -> list of {"role", "content"}."""
    data = _get(f"/api/history/{session_id}", client=client, base_url=base_url)
    return data.get("messages", [])


def is_opted_out(
    phone: str, client: httpx.Client | None = None, base_url: str | None = None
) -> bool:
    """True if the phone matches a Phase 1 opt-out session_id.

    Raises Phase1Unavailable if Phase 1 cannot be reached — callers
    must treat that as "opt-out status unknown" and fail closed.
    """
    wanted = normalize_phone(phone)
    for row in get_opt_outs(client=client, base_url=base_url):
        if normalize_phone(str(row.get("session_id", ""))) == wanted:
            return True
    return False
