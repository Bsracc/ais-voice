"""Transcript store for the AIS Phase 2 voice-browser server.

Text-only, append-only. NO audio is ever stored here — recording is OFF
by design (PHASE2_PLAN.md §3.2 item 7: California + Florida are
all-party-consent states; CA $5,000 per recording violation).

Each entry: {"role": "ai" | "user", "text": str, "created_at": ISO-8601 UTC}.
Entries live in memory and are appended to data/transcript.jsonl (one
JSON object per line) so a server restart does not lose the conversation.
"""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

_entries: list[dict] = []
_lock = threading.Lock()
_file: Path | None = None


def configure(path: Path | str) -> None:
    """Point the store at a transcript file (default data/transcript.jsonl)."""
    global _file
    _file = Path(path)
    _file.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        _entries.clear()
        if _file.exists():
            for line in _file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    _entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue


def append_transcript(role: str, text: str) -> dict:
    """Append one transcript entry to memory AND the JSONL file."""
    if role not in ("ai", "user"):
        raise ValueError(f"role must be 'ai' or 'user', got {role!r}")
    entry = {
        "role": role,
        "text": text,
        "created_at": datetime.now(UTC).isoformat(),
    }
    with _lock:
        _entries.append(entry)
        if _file is not None:
            with _file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def get_transcript() -> list[dict]:
    """Return a copy of all transcript entries, oldest first."""
    with _lock:
        return list(_entries)


def clear_transcript() -> None:
    """Clear memory only (used by tests). The JSONL file is append-only."""
    with _lock:
        _entries.clear()
