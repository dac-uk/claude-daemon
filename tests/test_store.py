"""Tests for the ConversationStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_daemon.memory.store import ConversationStore


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    """Create a fresh ConversationStore for each test."""
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


def test_create_conversation(store: ConversationStore):
    """Test creating a new conversation."""
    conv = store.get_or_create_conversation(None, "telegram", "user123")
    assert conv["platform"] == "telegram"
    assert conv["user_id"] == "user123"
    assert conv["status"] == "active"
    assert conv["message_count"] == 0
    assert len(conv["session_id"]) > 0


def test_get_existing_conversation(store: ConversationStore):
    """Test that getting a conversation returns the existing one."""
    conv1 = store.get_or_create_conversation(None, "telegram", "user123")
    conv2 = store.get_or_create_conversation(None, "telegram", "user123")
    assert conv1["id"] == conv2["id"]
    assert conv1["session_id"] == conv2["session_id"]


def test_session_isolation(store: ConversationStore):
    """Test that different platforms get different sessions."""
    tg = store.get_or_create_conversation(None, "telegram", "user123")
    dc = store.get_or_create_conversation(None, "discord", "user123")
    assert tg["session_id"] != dc["session_id"]


def test_add_and_get_messages(store: ConversationStore):
    """Test adding and retrieving messages."""
    conv = store.get_or_create_conversation(None, "cli", "local")
    store.add_message(conv["id"], "user", "Hello!")
    store.add_message(conv["id"], "assistant", "Hi there!", tokens=50, cost=0.01)

    msgs = store.get_recent_messages(conv["id"])
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Hello!"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "Hi there!"
    assert msgs[1]["tokens_used"] == 50


def test_conversation_text(store: ConversationStore):
    """Test getting formatted conversation text."""
    conv = store.get_or_create_conversation(None, "cli", "local")
    store.add_message(conv["id"], "user", "What is Python?")
    store.add_message(conv["id"], "assistant", "Python is a programming language.")

    text = store.get_conversation_text(conv["id"])
    assert "User: What is Python?" in text
    assert "Assistant: Python is a programming language." in text


def test_archive_and_reset(store: ConversationStore):
    """Test archiving and resetting conversations."""
    conv = store.get_or_create_conversation(None, "telegram", "user123")
    store.archive_conversation(conv["id"])

    # Should create a new conversation since the old one is archived
    conv2 = store.get_or_create_conversation(None, "telegram", "user123")
    assert conv2["id"] != conv["id"]


def test_summaries(store: ConversationStore):
    """Test adding and retrieving summaries."""
    conv = store.get_or_create_conversation(None, "cli", "local")
    store.add_summary(conv["id"], "This was a discussion about Python.", "session")

    summary = store.get_latest_summary(conv["id"])
    assert summary == "This was a discussion about Python."

    summaries = store.get_summaries_by_type("session")
    assert len(summaries) == 1


def test_stats(store: ConversationStore):
    """Test getting overall statistics."""
    store.get_or_create_conversation(None, "telegram", "user1")
    store.get_or_create_conversation(None, "discord", "user2")

    stats = store.get_stats()
    assert stats["total"] == 2
    assert stats["active"] == 2
