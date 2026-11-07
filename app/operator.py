"""Operator turn queue for muse-operator mode.

Honest fact: Muse has NO API endpoint and NO API key that code can call.
In muse-operator mode (BRAIN_MODE=muse-operator) incoming caller turns
are therefore NOT answered by any API. Each finished user turn is stored
here and waits until a human/AI operator in this chat reads the queue and
replies manually (see scripts/muse_reply.py in Phase 1 for the pattern).
This module never calls any API on Muse's behalf.

Turn-based, not session-based: the voice service has no persistent
session id, so every finished user turn is its own queue item.

Storage is one JSON file holding a list of turns, written atomically
(temp file + os.replace) under a threading lock. No SQLite, no new
dependencies. The server watcher (not this module) creates turns from
``built.context.messages`` — in Pipecat 1.15 the user aggregator consumes
TranscriptionFrame, so the transcript only reaches the LLM context.
"""

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

# Exact text spoken/shown while a turn waits for an operator reply.
HOLDING_TEXT = (
    "Thanks — your message is queued for a human operator, "
    "who will reply shortly."
)

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "operator_turns.json"

_lock = threading.Lock()
_path: Path = _DEFAULT_PATH
_turns: list[dict] = []


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _persist_locked() -> None:
    """Write the current turns atomically. Caller must hold ``_lock``."""
    _path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _path.with_suffix(_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(_turns, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, _path)


def configure(path) -> None:
    """Point the store at ``path`` (str or Path); create its parent dir and
    load any turns already stored there."""
    global _path, _turns
    with _lock:
        _path = Path(path)
        _path.parent.mkdir(parents=True, exist_ok=True)
        if _path.exists():
            raw = _path.read_text(encoding="utf-8").strip()
            _turns = json.loads(raw) if raw else []
        else:
            _turns = []


def create_turn(text: str) -> dict:
    """Queue one finished user turn as pending and return it."""
    turn = {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "created_at": _now_iso(),
        "status": "pending",
        "reply": None,
        "replied_at": None,
    }
    with _lock:
        _turns.append(turn)
        _persist_locked()
    return dict(turn)


def list_pending() -> list[dict]:
    """Pending turns only, oldest first."""
    with _lock:
        return [dict(t) for t in _turns if t["status"] == "pending"]


def list_turns() -> list[dict]:
    """All turns, oldest first."""
    with _lock:
        return [dict(t) for t in _turns]


def get_turn(turn_id: str) -> dict | None:
    """One turn by id, or None when unknown."""
    with _lock:
        for turn in _turns:
            if turn["id"] == turn_id:
                return dict(turn)
    return None


def save_operator_reply(turn_id: str, reply: str) -> bool:
    """Store an operator reply for a pending turn.

    Returns True when stored, False when the turn is unknown, already
    answered, or the reply is blank. Nothing is stored on False, so a
    second reply cannot double-answer a turn.
    """
    if not reply or not reply.strip():
        return False
    with _lock:
        for turn in _turns:
            if turn["id"] == turn_id:
                if turn["status"] != "pending":
                    return False
                turn["status"] = "answered"
                turn["reply"] = reply.strip()
                turn["replied_at"] = _now_iso()
                _persist_locked()
                return True
    return False


def clear() -> None:
    """Empty the in-memory queue only (for tests). The JSON file is left
    untouched, so ``configure`` on the same path reloads the stored turns."""
    with _lock:
        _turns.clear()
