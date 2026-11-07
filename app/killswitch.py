"""Kill switch for Phase 2 voice dialing.

State lives in data/kill_switch.json: {"engaged": bool, ...}.
Default is NOT engaged. A missing file means not engaged; a corrupt
file is treated as ENGAGED (fail-closed: a broken switch must stop
dialing, not allow it).

The server (built by the voice-service side) calls engage() to stop
all dialing immediately; gates.check_eligibility() reads the state
on every check, so engaging takes effect on the very next check.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def default_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "kill_switch.json"


def load_state(path: Path | str | None = None) -> dict:
    path = Path(path) if path is not None else default_path()
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return {"engaged": False}
    except (json.JSONDecodeError, OSError):
        # Corrupt/unreadable switch file: fail closed (stop dialing).
        return {"engaged": True, "error": "unreadable kill_switch.json"}
    if not isinstance(state, dict):
        return {"engaged": True, "error": "kill_switch.json is not an object"}
    return state


def is_engaged(path: Path | str | None = None) -> bool:
    return bool(load_state(path).get("engaged", False))


def _save(path: Path, state: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(UTC).isoformat()
    path.write_text(json.dumps(state, indent=2) + "\n")
    return state


def engage(path: Path | str | None = None, note: str | None = None) -> dict:
    """Stop all dialing immediately. Returns the new state."""
    path = Path(path) if path is not None else default_path()
    state = load_state(path)
    state["engaged"] = True
    if note is not None:
        state["note"] = note
    return _save(path, state)


def disengage(path: Path | str | None = None) -> dict:
    """Re-enable dialing (gates still apply). Returns the new state."""
    path = Path(path) if path is not None else default_path()
    state = load_state(path)
    state["engaged"] = False
    state.pop("note", None)
    return _save(path, state)
