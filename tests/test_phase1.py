"""Tests for app.phase1 — read-only HTTP client to Phase 1 (port 7860).

Read-only: GET /api/health, /api/opt-outs, /api/history/<id> only.
Never writes to the Phase 1 DB and never touches data/sales.db.
"""

import httpx
import pytest

from app.phase1 import (
    Phase1Unavailable,
    get_history,
    get_opt_outs,
    health,
    is_opted_out,
)

PHONE = "+1 (555) 010-0100"  # fictional 555 test number


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def _opt_outs_handler(request):
    assert request.url.path == "/api/opt-outs"
    return httpx.Response(
        200,
        json={
            "opt_outs": [{"session_id": "+15550100100", "created_at": "2026-11-06 21:00:18"}],
        },
    )


def test_get_opt_outs():
    out = get_opt_outs(client=_client(_opt_outs_handler))
    assert out == [{"session_id": "+15550100100", "created_at": "2026-11-06 21:00:18"}]


def test_is_opted_out_matches_normalized_phone():
    client = _client(_opt_outs_handler)
    assert is_opted_out(PHONE, client=client) is True
    assert is_opted_out("+1-555-010-0199", client=_client(_opt_outs_handler)) is False


def test_get_history():
    def handler(request):
        assert request.url.path == "/api/history/sess-1"
        return httpx.Response(
            200,
            json={
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ]
            },
        )

    assert get_history("sess-1", client=_client(handler)) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_health():
    def handler(request):
        assert request.url.path == "/api/health"
        return httpx.Response(200, json={"status": "ok", "mode": "demo", "phase": 1})

    assert health(client=_client(handler))["status"] == "ok"


def test_connection_error_raises_phase1_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(Phase1Unavailable):
        get_opt_outs(client=_client(handler))
    with pytest.raises(Phase1Unavailable):
        health(client=_client(handler))


def test_server_error_raises_phase1_unavailable():
    def handler(request):
        return httpx.Response(500, json={"detail": "boom"})

    with pytest.raises(Phase1Unavailable):
        get_opt_outs(client=_client(handler))


def test_live_health_if_phase1_running():
    """Live check against the real Phase 1 dashboard (skips if down)."""
    try:
        result = health()
    except Phase1Unavailable:
        pytest.skip("Phase 1 not reachable on 127.0.0.1:7860")
    assert result["status"] == "ok"


def test_live_opt_outs_shape_if_phase1_running():
    try:
        out = get_opt_outs()
    except Phase1Unavailable:
        pytest.skip("Phase 1 not reachable on 127.0.0.1:7860")
    assert isinstance(out, list)
    assert all("session_id" in row for row in out)
