"""Operator turn queue (muse-operator mode): turn-based, JSON file store."""

import json

import pytest

from app import operator


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "nested" / "turns.json"
    operator.configure(str(path))
    operator.clear()
    yield path
    operator.clear()


def test_holding_text_exact():
    assert operator.HOLDING_TEXT == (
        "Thanks — your message is queued for a human operator, "
        "who will reply shortly."
    )


def test_configure_creates_parent_dir(store):
    assert store.parent.is_dir()


def test_create_turn_fields(store):
    turn = operator.create_turn("Hello, I need a new website.")
    assert len(turn["id"]) == 12
    assert all(c in "0123456789abcdef" for c in turn["id"])
    assert turn["text"] == "Hello, I need a new website."
    assert turn["status"] == "pending"
    assert turn["reply"] is None
    assert turn["replied_at"] is None
    assert turn["created_at"]  # ISO-8601 UTC timestamp
    assert set(turn) == {"id", "text", "created_at", "status", "reply", "replied_at"}


def test_create_ids_are_unique(store):
    ids = {operator.create_turn(f"msg {i}")["id"] for i in range(5)}
    assert len(ids) == 5


def test_list_turns_oldest_first(store):
    first = operator.create_turn("first")
    second = operator.create_turn("second")
    assert [t["id"] for t in operator.list_turns()] == [first["id"], second["id"]]


def test_list_pending_only_pending_oldest_first(store):
    first = operator.create_turn("first")
    second = operator.create_turn("second")
    operator.save_operator_reply(first["id"], "answered")
    assert [t["id"] for t in operator.list_pending()] == [second["id"]]
    assert [t["id"] for t in operator.list_turns()] == [first["id"], second["id"]]


def test_get_turn_known_and_unknown(store):
    turn = operator.create_turn("hello")
    assert operator.get_turn(turn["id"]) == turn
    assert operator.get_turn("doesnotexist") is None


def test_save_reply_success(store):
    turn = operator.create_turn("hello")
    assert operator.save_operator_reply(turn["id"], "  Yes, we can help.  ") is True
    saved = operator.get_turn(turn["id"])
    assert saved["status"] == "answered"
    assert saved["reply"] == "Yes, we can help."
    assert saved["replied_at"]


def test_second_reply_rejected_and_unchanged(store):
    turn = operator.create_turn("hello")
    operator.save_operator_reply(turn["id"], "first reply")
    before = operator.get_turn(turn["id"])
    assert operator.save_operator_reply(turn["id"], "second reply") is False
    assert operator.get_turn(turn["id"]) == before


def test_blank_reply_rejected(store):
    turn = operator.create_turn("hello")
    assert operator.save_operator_reply(turn["id"], "") is False
    assert operator.save_operator_reply(turn["id"], "   ") is False
    assert operator.get_turn(turn["id"])["status"] == "pending"


def test_unknown_turn_rejected(store):
    assert operator.save_operator_reply("doesnotexist", "hello") is False


def test_persists_to_json_file_and_configure_reloads(store):
    first = operator.create_turn("first")
    second = operator.create_turn("second")
    on_disk = json.loads(store.read_text())
    assert [t["id"] for t in on_disk] == [first["id"], second["id"]]
    operator.clear()
    assert operator.list_turns() == []
    operator.configure(str(store))
    assert [t["id"] for t in operator.list_turns()] == [first["id"], second["id"]]
    assert operator.list_pending()[0]["text"] == "first"


def test_clear_empties_memory_only(store):
    operator.create_turn("hello")
    operator.clear()
    assert operator.list_turns() == []
    assert operator.list_pending() == []
