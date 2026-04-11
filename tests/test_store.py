"""Tests for the ConversationStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_daemon.memory.store import ConversationStore


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


def test_create_conversation(store: ConversationStore):
    conv = store.get_or_create_conversation(None, "telegram", "user123")
    assert conv["platform"] == "telegram"
    assert conv["user_id"] == "user123"
    assert conv["status"] == "active"
    assert conv["message_count"] == 0
    assert len(conv["session_id"]) > 0


def test_get_existing_conversation(store: ConversationStore):
    conv1 = store.get_or_create_conversation(None, "telegram", "user123")
    conv2 = store.get_or_create_conversation(None, "telegram", "user123")
    assert conv1["id"] == conv2["id"]


def test_cross_platform_session_sharing(store: ConversationStore):
    """Sessions are shared across platforms for the same user — enables seamless handover."""
    tg = store.get_or_create_conversation(None, "telegram", "user123")
    dc = store.get_or_create_conversation(None, "discord", "user123")
    assert tg["session_id"] == dc["session_id"]  # Same user, shared session


def test_different_users_isolated(store: ConversationStore):
    """Different users should get separate sessions."""
    u1 = store.get_or_create_conversation(None, "telegram", "user1")
    u2 = store.get_or_create_conversation(None, "telegram", "user2")
    assert u1["session_id"] != u2["session_id"]


def test_get_conversation_by_session(store: ConversationStore):
    conv = store.get_or_create_conversation(None, "cli", "local")
    found = store.get_conversation_by_session(conv["session_id"])
    assert found is not None
    assert found["id"] == conv["id"]

    missing = store.get_conversation_by_session("nonexistent")
    assert missing is None


def test_add_and_get_messages(store: ConversationStore):
    conv = store.get_or_create_conversation(None, "cli", "local")
    store.add_message(conv["id"], "user", "Hello!")
    store.add_message(conv["id"], "assistant", "Hi there!", tokens=50, cost=0.01)

    msgs = store.get_recent_messages(conv["id"])
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Hello!"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["tokens_used"] == 50


def test_conversation_text_full_content(store: ConversationStore):
    """Test that conversation text returns full content, not truncated."""
    conv = store.get_or_create_conversation(None, "cli", "local")
    long_content = "x" * 500
    store.add_message(conv["id"], "user", long_content)
    store.add_message(conv["id"], "assistant", "Short reply.")

    text = store.get_conversation_text(conv["id"])
    assert long_content in text


def test_archive_and_reset(store: ConversationStore):
    conv = store.get_or_create_conversation(None, "telegram", "user123")
    store.archive_conversation(conv["id"])

    conv2 = store.get_or_create_conversation(None, "telegram", "user123")
    assert conv2["id"] != conv["id"]


def test_reset_conversation(store: ConversationStore):
    conv = store.get_or_create_conversation(None, "telegram", "user123")
    store.reset_conversation("user123", "telegram")

    conv2 = store.get_or_create_conversation(None, "telegram", "user123")
    assert conv2["id"] != conv["id"]


def test_summaries(store: ConversationStore):
    conv = store.get_or_create_conversation(None, "cli", "local")
    store.add_summary(conv["id"], "Discussion about Python.", "light_sleep")
    store.add_summary(conv["id"], "Deeper analysis.", "deep_sleep")

    summary = store.get_latest_summary(conv["id"])
    assert summary == "Deeper analysis."

    light = store.get_summaries_by_type("light_sleep")
    assert len(light) == 1
    deep = store.get_summaries_by_type("deep_sleep")
    assert len(deep) == 1


def test_stats(store: ConversationStore):
    store.get_or_create_conversation(None, "telegram", "user1")
    store.get_or_create_conversation(None, "discord", "user2")

    stats = store.get_stats()
    assert stats["total"] == 2
    assert stats["active"] == 2


def test_user_stats(store: ConversationStore):
    conv = store.get_or_create_conversation(None, "telegram", "user1")
    store.add_message(conv["id"], "user", "test")
    store.update_conversation(conv["id"], cost=0.05)

    stats = store.get_user_stats("user1", "telegram")
    assert stats["sessions"] == 1
    assert stats["total_cost"] == 0.05
