"""Tests for the AIS Phase 2 voice-browser server (app/server.py).

TDD: these tests were written before the implementation.
Run:  .venv/bin/python -m pytest tests/test_server.py -v
"""

import asyncio
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pipecat.frames.frames import TextFrame

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import server as server_mod
from app.transcript import append_transcript, clear_transcript, get_transcript


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Fresh app with a temp transcript file per test."""
    transcript_file = tmp_path / "transcript.jsonl"
    monkeypatch.setattr(server_mod, "TRANSCRIPT_PATH", transcript_file)
    monkeypatch.setattr(server_mod, "transcript_store", None)
    # Keep the kill-switch test hermetic: never touch real data/.
    monkeypatch.setattr(server_mod, "KILL_SWITCH_PATH", tmp_path / "kill_switch.json")
    # Hermetic keys: no live voice in tests even if the shell has keys.
    for key in server_mod.REQUIRED_LIVE_KEYS:
        monkeypatch.setenv(key, "")
    server_mod.reset_state()
    clear_transcript()
    with TestClient(server_mod.app) as c:
        yield c
    server_mod.reset_state()
    clear_transcript()


def test_health_200(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mode"] == "voice-browser"
    assert body["phase"] == 2
    assert body["recording"] is False


def test_status_recording_false_and_idle(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["recording"] is False
    assert body["state"] == "idle"
    assert body["mode"] == "voice-browser"
    assert body["phase"] == 2
    # Compliance gates are visible even though no dialling happens here.
    assert body["gates"]["recording_off"] is True
    assert body["gates"]["dial_enabled"] is False
    # No keys in this environment: live voice is honestly unavailable.
    assert body["live_ready"] is False
    assert "missing" in body["demo_reason"]


def test_status_transport_field_null_when_idle(client):
    # /ws/voice reports the active transport ("webrtc"/"websocket"/null).
    assert client.get("/api/status").json()["transport"] is None


def test_transcript_empty_then_filled(client):
    r = client.get("/api/transcript")
    assert r.status_code == 200
    assert r.json() == []

    append_transcript("ai", "Hello, I am an AI assistant calling on behalf of AISIMA.")
    append_transcript("user", "Hi, tell me about the redesign.")

    r = client.get("/api/transcript")
    assert r.status_code == 200
    entries = r.json()
    assert len(entries) == 2
    assert entries[0]["role"] == "ai"
    assert "AISIMA" in entries[0]["text"]
    assert entries[1]["role"] == "user"
    for entry in entries:
        assert set(entry.keys()) == {"role", "text", "created_at"}
        assert entry["created_at"]


def test_transcript_persists_to_jsonl(client, tmp_path):
    append_transcript("ai", "Test line one.")
    # A fresh read of the file the server was pointed at must show the line.
    lines = server_mod.TRANSCRIPT_PATH.read_text().strip().splitlines()
    assert len(lines) == 1
    import json

    row = json.loads(lines[0])
    assert row["role"] == "ai"
    assert row["text"] == "Test line one."


def test_stop_endpoint_kill_switch(client):
    r = client.post("/api/stop")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "stopped"
    assert body["state"] == "idle"
    # Status must agree after the kill switch.
    assert client.get("/api/status").json()["state"] == "idle"


def test_index_page_has_mic_button_and_disclosure(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    html = r.text
    # Mic start/stop control.
    assert "Start" in html
    assert "mic" in html.lower()
    # AI disclosure visible on the page (first-5-seconds rule, PLAN §3.2 item 6).
    assert "AI" in html
    assert "AISIMA" in html
    # Simulation warning — this is a browser test, NOT a telephone call.
    assert "Simulation" in html
    assert "no telephone call" in html
    # Recording is off — stated on the page.
    assert "not recorded" in html.lower() or "recording" in html.lower()


def test_no_recording_endpoint(client):
    # Recording audio is OFF by design (PLAN §3.2 item 7). There must be
    # no endpoint that accepts audio uploads.
    r = client.post("/api/record", json={})
    assert r.status_code == 404
    r = client.post("/api/audio", json={})
    assert r.status_code == 404


def test_start_without_keys_is_503_demo_not_crash(client):
    # No API keys: starting must NOT crash the server, and must NOT
    # pretend live voice is ready. It answers 503 demo/unavailable.
    r = client.post("/api/start")
    assert r.status_code == 503
    assert "demo" in r.json()["detail"]
    assert client.get("/api/status").json()["recording"] is False


def test_offer_without_keys_is_503_and_creates_no_connection(client):
    # Demo mode answers 503 BEFORE any peer connection is created, so a
    # later real offer is not blocked by an orphaned connection (SINGLE
    # mode would otherwise answer the next offer with a 400).
    body = {"sdp": "v=0", "type": "offer"}
    assert client.post("/api/offer", json=body).status_code == 503
    assert client.post("/api/offer", json=body).status_code == 503
    assert client.patch("/api/offer", json={"pc_id": "x", "candidates": []}).status_code == 503


def test_start_session_without_keys_raises(client):
    # The session starter refuses demo mode before touching any transport.
    with pytest.raises(RuntimeError, match="not live-ready"):
        asyncio.run(server_mod.start_session(connection=None))


def test_sanitize_no_proxy_drops_bracketed_ipv6(monkeypatch):
    # Regression: the VM's no_proxy contains "[::1]", which httpx (Anthropic
    # SDK) cannot parse — service construction raised InvalidURL before any
    # key was used. Plain "::1" already covers the same host.
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1,[::1],[fd8b::1]")
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")
    server_mod._sanitize_no_proxy()
    import os

    assert os.environ["no_proxy"] == "localhost,127.0.0.1,::1"
    assert os.environ["NO_PROXY"] == "localhost"


def test_transcript_watcher_picks_up_context_turns(client):
    # Fake LLMContext: system prompt + user turn + assistant turn, then a
    # second user turn appended mid-watch. The greeting text is skipped
    # (it is appended directly by start_session).
    greeting = "Hi, I am an AI assistant calling on behalf of AISIMA."
    messages = [
        {"role": "system", "content": "You are AISIMA."},
        {"role": "user", "content": "Hello, tell me about the redesign."},
        {"role": "assistant", "content": greeting},
        {"role": "assistant", "content": "It costs $1,000 one-time."},
    ]

    class FakeContext:
        def __init__(self, msgs):
            self.messages = msgs

    context = FakeContext(messages)

    async def _run():
        server_mod._state["state"] = "active"

        async def stopper():
            await asyncio.sleep(0.1)
            server_mod._state["state"] = "idle"

        await asyncio.gather(
            server_mod._watch_transcript(context, greeting, interval=0.02),
            stopper(),
        )

    asyncio.run(_run())

    entries = get_transcript()
    assert [(e["role"], e["text"]) for e in entries] == [
        ("user", "Hello, tell me about the redesign."),
        ("ai", "It costs $1,000 one-time."),
    ]

# ---------------------------------------------------------------------------
# muse-operator mode (BRAIN_MODE=muse-operator) — worker B tests.
# app/operator.py is built by worker A; until it lands, these tests run
# against _FakeOperator below, which implements the same contracted API:
# HOLDING_TEXT, configure(path), create_turn(text), list_pending(),
# list_turns(), get_turn(id), save_operator_reply(id, reply), clear().
# ---------------------------------------------------------------------------


class _FakeOperator:
    """In-memory stand-in for app/operator.py (contracted API only)."""

    HOLDING_TEXT = (
        "Thanks \u2014 your message is queued for a human operator, "
        "who will reply shortly."
    )

    def __init__(self):
        self._turns: dict[str, dict] = {}
        self._seq = 0
        self.path = None

    def configure(self, path):
        self.path = Path(path)
        self._turns.clear()
        self._seq = 0

    def create_turn(self, text):
        self._seq += 1
        turn = {
            "id": f"turn-{self._seq}",
            "text": text,
            "created_at": "2026-11-07T00:00:00+00:00",
            "status": "pending",
            "reply": None,
            "replied_at": None,
        }
        self._turns[turn["id"]] = turn
        return dict(turn)

    def list_pending(self):
        return [dict(t) for t in self._turns.values() if t["status"] == "pending"]

    def list_turns(self):
        return [dict(t) for t in self._turns.values()]

    def get_turn(self, turn_id):
        turn = self._turns.get(turn_id)
        return dict(turn) if turn is not None else None

    def save_operator_reply(self, turn_id, reply):
        turn = self._turns.get(turn_id)
        if turn is None or turn["status"] != "pending":
            return False
        if not reply or not reply.strip():
            return False
        turn["status"] = "answered"
        turn["reply"] = reply.strip()
        turn["replied_at"] = "2026-11-07T00:01:00+00:00"
        return True

    def clear(self):
        self._turns.clear()
        self._seq = 0


class _FakeWorker:
    """Records frames queued for speaking (stands in for a pipeline worker)."""

    def __init__(self):
        self.queued = []

    async def queue_frame(self, frame):
        self.queued.append(frame)


@pytest.fixture()
def op_client(tmp_path, monkeypatch):
    """Fresh app with temp transcript / kill-switch / operator stores.

    Uses the real app/operator.py when worker A has delivered it,
    otherwise the fake implementing the same contracted API.
    """
    monkeypatch.setattr(server_mod, "TRANSCRIPT_PATH", tmp_path / "transcript.jsonl")
    monkeypatch.setattr(server_mod, "transcript_store", None)
    monkeypatch.setattr(server_mod, "KILL_SWITCH_PATH", tmp_path / "kill_switch.json")
    monkeypatch.setattr(server_mod, "OPERATOR_PATH", tmp_path / "operator_pending.json")
    for key in server_mod.REQUIRED_LIVE_KEYS:
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("BRAIN_MODE", "anthropic")
    if getattr(server_mod, "operator", None) is None:
        monkeypatch.setattr(server_mod, "operator", _FakeOperator())
    server_mod.reset_state()
    clear_transcript()
    with TestClient(server_mod.app) as c:
        yield c
    server_mod.reset_state()
    clear_transcript()


def _set_operator_keys(monkeypatch):
    """STT + TTS keys present, Anthropic deliberately empty."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("CARTESIA_API_KEY", "ct-test")
    monkeypatch.setenv("CARTESIA_VOICE_ID", "voice-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")


def test_status_operator_mode_live_ready_without_anthropic(op_client, monkeypatch):
    monkeypatch.setenv("BRAIN_MODE", "muse-operator")
    _set_operator_keys(monkeypatch)
    body = op_client.get("/api/status").json()
    assert body["live_ready"] is True
    assert body["brain_mode"] == "muse-operator"
    assert body["pending_count"] == 0


def test_status_anthropic_mode_without_anthropic_is_not_live_ready(
    op_client, monkeypatch
):
    monkeypatch.setenv("BRAIN_MODE", "anthropic")
    _set_operator_keys(monkeypatch)
    body = op_client.get("/api/status").json()
    assert body["live_ready"] is False
    assert body["brain_mode"] == "anthropic"
    assert "ANTHROPIC_API_KEY" in body["demo_reason"]


def test_status_invalid_brain_mode(op_client, monkeypatch):
    monkeypatch.setenv("BRAIN_MODE", "bogus")
    _set_operator_keys(monkeypatch)
    body = op_client.get("/api/status").json()
    assert body["live_ready"] is False
    # Settings() raises on an invalid BRAIN_MODE (config validator), so the
    # mode is reported as "unknown" and the reason carries the invalid value.
    assert body["brain_mode"] == "unknown"
    assert "invalid BRAIN_MODE" in body["demo_reason"]


def test_status_pending_count_tracks_turns(op_client):
    assert op_client.get("/api/status").json()["pending_count"] == 0
    server_mod.operator.create_turn("How much is the redesign?")
    assert op_client.get("/api/status").json()["pending_count"] == 1


def test_pending_empty_then_filled(op_client):
    assert op_client.get("/api/pending").json() == []
    turn = server_mod.operator.create_turn("How much is the redesign?")
    pending = op_client.get("/api/pending").json()
    assert len(pending) == 1
    assert pending[0]["id"] == turn["id"]
    assert pending[0]["text"] == "How much is the redesign?"

    r = op_client.get(f"/api/pending/{turn['id']}")
    assert r.status_code == 200
    assert r.json()["text"] == "How much is the redesign?"
    assert op_client.get("/api/pending/nope").status_code == 404


def test_operator_reply_200_saves_and_transcribes(op_client):
    turn = server_mod.operator.create_turn("How much is the redesign?")
    r = op_client.post(
        "/api/operator/reply",
        json={"turn_id": turn["id"], "reply": "It costs $1,000 one-time."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered"
    assert body["turn_id"] == turn["id"]
    assert body["spoken"] is False  # no active session in this test
    # Saved: no longer pending, reply visible on the turn.
    assert op_client.get("/api/pending").json() == []
    saved = op_client.get(f"/api/pending/{turn['id']}").json()
    assert saved["reply"] == "It costs $1,000 one-time."
    # Transcribed as an AI entry.
    entries = get_transcript()
    assert ("ai", "It costs $1,000 one-time.") in [
        (e["role"], e["text"]) for e in entries
    ]


def test_operator_reply_404_unknown_turn(op_client):
    r = op_client.post(
        "/api/operator/reply", json={"turn_id": "nope", "reply": "Hello."}
    )
    assert r.status_code == 404


def test_operator_reply_409_second_reply(op_client):
    turn = server_mod.operator.create_turn("How much is the redesign?")
    payload = {"turn_id": turn["id"], "reply": "It costs $1,000 one-time."}
    assert op_client.post("/api/operator/reply", json=payload).status_code == 200
    assert op_client.post("/api/operator/reply", json=payload).status_code == 409


def test_operator_reply_400_empty_reply(op_client):
    turn = server_mod.operator.create_turn("How much is the redesign?")
    for bad in ("", "   "):
        r = op_client.post(
            "/api/operator/reply", json={"turn_id": turn["id"], "reply": bad}
        )
        assert r.status_code == 400
    # Still pending after the refused replies.
    assert len(op_client.get("/api/pending").json()) == 1


def test_operator_reply_spoken_when_session_active(op_client):
    worker = _FakeWorker()
    server_mod._state["state"] = "active"
    server_mod._active_worker = worker
    turn = server_mod.operator.create_turn("How much is the redesign?")
    r = op_client.post(
        "/api/operator/reply",
        json={"turn_id": turn["id"], "reply": "It costs $1,000 one-time."},
    )
    assert r.status_code == 200
    assert r.json()["spoken"] is True
    assert len(worker.queued) == 1
    assert isinstance(worker.queued[0], TextFrame)
    assert worker.queued[0].text == "It costs $1,000 one-time."


def test_watcher_operator_mode_creates_turn_and_holding(op_client):
    greeting = "Hi, I am an AI assistant calling on behalf of AISIMA."
    messages = [
        {"role": "system", "content": "You are AISIMA."},
        {"role": "user", "content": "How much is the redesign?"},
        {"role": "assistant", "content": greeting},
    ]

    class FakeContext:
        def __init__(self, msgs):
            self.messages = msgs

    context = FakeContext(messages)
    worker = _FakeWorker()

    async def _run():
        server_mod._state["state"] = "active"

        async def stopper():
            await asyncio.sleep(0.1)
            server_mod._state["state"] = "idle"

        await asyncio.gather(
            server_mod._watch_transcript(
                context, greeting, interval=0.02, worker=worker, operator_mode=True
            ),
            stopper(),
        )

    asyncio.run(_run())

    entries = get_transcript()
    assert [(e["role"], e["text"]) for e in entries] == [
        ("user", "How much is the redesign?"),
    ]
    pending = server_mod.operator.list_pending()
    assert len(pending) == 1
    assert pending[0]["text"] == "How much is the redesign?"
    assert len(worker.queued) == 1
    assert isinstance(worker.queued[0], TextFrame)
    assert worker.queued[0].text == server_mod.operator.HOLDING_TEXT


def test_watcher_anthropic_mode_creates_no_turn(op_client):
    greeting = "Hi, I am an AI assistant calling on behalf of AISIMA."
    messages = [
        {"role": "user", "content": "How much is the redesign?"},
    ]

    class FakeContext:
        def __init__(self, msgs):
            self.messages = msgs

    context = FakeContext(messages)

    async def _run():
        server_mod._state["state"] = "active"

        async def stopper():
            await asyncio.sleep(0.1)
            server_mod._state["state"] = "idle"

        await asyncio.gather(
            server_mod._watch_transcript(context, greeting, interval=0.02),
            stopper(),
        )

    asyncio.run(_run())

    assert server_mod.operator.list_pending() == []
    assert [(e["role"], e["text"]) for e in get_transcript()] == [
        ("user", "How much is the redesign?"),
    ]


def test_watcher_dedupes_repeated_assistant_entry(op_client):
    # The operator reply endpoint appends the reply directly; when the
    # same text surfaces again via context the watcher must not duplicate it.
    greeting = "Hi, I am an AI assistant calling on behalf of AISIMA."
    append_transcript("ai", "It costs $1,000 one-time.")
    messages = [
        {"role": "assistant", "content": "It costs $1,000 one-time."},
        {"role": "assistant", "content": "Anything else I can help with?"},
    ]

    class FakeContext:
        def __init__(self, msgs):
            self.messages = msgs

    context = FakeContext(messages)

    async def _run():
        server_mod._state["state"] = "active"

        async def stopper():
            await asyncio.sleep(0.1)
            server_mod._state["state"] = "idle"

        await asyncio.gather(
            server_mod._watch_transcript(context, greeting, interval=0.02),
            stopper(),
        )

    asyncio.run(_run())

    assert [(e["role"], e["text"]) for e in get_transcript()] == [
        ("ai", "It costs $1,000 one-time."),
        ("ai", "Anything else I can help with?"),
    ]


def test_index_page_shows_brain_mode_and_operator_note(op_client):
    r = op_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'id="brain-mode"' in html
    assert "operator" in html.lower()


# --- scripts/muse_reply.py (operator CLI, HTTP only, no Muse API) ---


def _load_cli():
    path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "scripts", "muse_reply.py")
    )
    spec = importlib.util.spec_from_file_location("voice_muse_reply", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeHTTPResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_cli_reply_without_turn_exits_2():
    cli = _load_cli()
    assert cli.main(["--reply", "hello"]) == 2


def test_cli_list_empty(monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=10: _FakeHTTPResponse(200, "[]"),
    )
    assert cli.main(["--list"]) == 0


def test_cli_list_shows_turns(monkeypatch):
    cli = _load_cli()
    body = json.dumps(
        [{"id": "turn-1", "text": "How much?", "created_at": "2026-11-07T00:00:00+00:00"}]
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=10: _FakeHTTPResponse(200, body),
    )
    assert cli.main(["--list"]) == 0


def test_cli_turn_show_404(monkeypatch):
    cli = _load_cli()

    def _raise(req, timeout=10):
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, None
        )

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    assert cli.main(["--turn", "nope"]) == 1


def test_cli_reply_409(monkeypatch):
    cli = _load_cli()

    def _raise(req, timeout=10):
        import io

        raise urllib.error.HTTPError(
            req.full_url,
            409,
            "Conflict",
            {},
            io.BytesIO(b'{"detail": "turn already answered: turn-1"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    assert cli.main(["--turn", "turn-1", "--reply", "again"]) == 1


def test_cli_server_down_is_clear_error_not_traceback(monkeypatch):
    cli = _load_cli()

    def _raise(req, timeout=10):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    assert cli.main(["--list"]) == 1
