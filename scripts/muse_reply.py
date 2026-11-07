#!/usr/bin/env python3
"""Operator CLI for the AIS voice service (muse-operator mode).

This script does NOT contact Muse: Muse has no API endpoint and no API
key that code can call. It only talks to the local voice server over HTTP
(default http://127.0.0.1:7861):

    python scripts/muse_reply.py --list
        Show turns waiting for an operator reply (GET /api/pending).

    python scripts/muse_reply.py --turn <id>
        Show one turn (GET /api/pending/<id>).

    python scripts/muse_reply.py --turn <id> --reply "<text>"
        Save the operator reply (POST /api/operator/reply). The server
        speaks it aloud when a voice session is active; otherwise the
        reply is stored and shown in the transcript.

    python scripts/muse_reply.py --list --base-url http://127.0.0.1:7861
        Talk to a server on a different base URL.

The voice server must be running; without it this script prints a clear
error instead of a raw traceback. Replies are manual and not instant —
the operator must be online.
"""

import argparse
import json
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:7861"


class ServerUnreachableError(Exception):
    """The voice server could not be reached at all (not running/refused)."""


def _request(method: str, url: str, payload: dict | None = None) -> tuple[int, object]:
    """One HTTP call. Returns (status, parsed JSON body).

    HTTP errors (404/409/400/...) return their status + parsed body
    instead of raising; only a refused/failed connection raises
    ServerUnreachableError.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"detail": raw or exc.reason}
        return exc.code, body
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ServerUnreachableError(str(exc)) from exc


def _server_message(body: object) -> str:
    """Human-readable message from a server response body."""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        return json.dumps(body)
    return str(body)


def _cmd_list(base_url: str) -> int:
    try:
        status, body = _request("GET", f"{base_url}/api/pending")
    except ServerUnreachableError as exc:
        print(f"Error: cannot reach the voice server at {base_url} — is it running? ({exc})")
        return 1
    if status != 200:
        print(f"Error: server answered {status}: {_server_message(body)}")
        return 1
    turns = body if isinstance(body, list) else []
    if not turns:
        print("No pending turns. The operator queue is empty.")
        return 0
    print(f"{len(turns)} turn(s) waiting for an operator reply:\n")
    for i, turn in enumerate(turns, start=1):
        print(f"{i}. turn: {turn.get('id')}")
        print(f"   user ({turn.get('created_at', '?')}): {turn.get('text', '')}")
        print(
            f"   reply with: python scripts/muse_reply.py "
            f"--turn {turn.get('id')} --reply \"...\""
        )
    return 0


def _cmd_show(base_url: str, turn_id: str) -> int:
    try:
        status, body = _request("GET", f"{base_url}/api/pending/{turn_id}")
    except ServerUnreachableError as exc:
        print(f"Error: cannot reach the voice server at {base_url} — is it running? ({exc})")
        return 1
    if status == 404:
        print(f"No turn '{turn_id}': {_server_message(body)}")
        return 1
    if status != 200:
        print(f"Error: server answered {status}: {_server_message(body)}")
        return 1
    turn = body if isinstance(body, dict) else {}
    print(f"Turn: {turn.get('id')}")
    print(f"Status: {turn.get('status', '?')}")
    print(f"User ({turn.get('created_at', '?')}): {turn.get('text', '')}")
    if turn.get("reply"):
        print(f"Operator reply ({turn.get('replied_at', '?')}): {turn.get('reply')}")
    else:
        print(
            f"\nReply with: python scripts/muse_reply.py "
            f"--turn {turn.get('id')} --reply \"...\""
        )
    return 0


def _cmd_reply(base_url: str, turn_id: str, reply: str) -> int:
    try:
        status, body = _request(
            "POST",
            f"{base_url}/api/operator/reply",
            {"turn_id": turn_id, "reply": reply},
        )
    except ServerUnreachableError as exc:
        print(f"Error: cannot reach the voice server at {base_url} — is it running? ({exc})")
        return 1
    if status == 200:
        spoken = body.get("spoken") if isinstance(body, dict) else None
        print(f"Reply saved for turn '{turn_id}'.")
        if spoken:
            print("Spoken aloud to the caller (a voice session is active).")
        else:
            print("No voice session active — the reply is stored and shown in the transcript.")
        return 0
    print(f"NOT saved ({status}): {_server_message(body)}")
    print("Run --list to see which turns are still waiting.")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="muse_reply.py",
        description="Operator queue for muse-operator mode: list pending "
                    "caller turns and save manual operator replies. "
                    "Talks to the local voice server over HTTP only; "
                    "it never contacts Muse and needs no API key.",
        epilog="Example flow: --list, then --turn <id> to read the turn, "
               "then --turn <id> --reply \"...\" to answer. "
               "Replies are manual and not instant; the operator must be online.",
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help="voice server base URL (default %(default)s)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="list turns waiting for an operator reply",
    )
    parser.add_argument(
        "--turn", metavar="TURN_ID",
        help="show one turn (add --reply to answer it)",
    )
    parser.add_argument(
        "--reply", metavar="TEXT",
        help="operator reply text to save for --turn (requires --turn)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_url = args.base_url.rstrip("/")
    if args.reply is not None and args.turn is None:
        print("Error: --reply needs --turn <id>.")
        return 2
    if args.turn is not None:
        if args.reply is not None:
            return _cmd_reply(base_url, args.turn, args.reply)
        return _cmd_show(base_url, args.turn)
    if args.list:
        return _cmd_list(base_url)
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
