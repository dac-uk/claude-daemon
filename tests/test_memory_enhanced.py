"""Tests for enhanced memory features: validation, FTS5 search, agent metrics."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_daemon.memory.durable import DurableMemory
from claude_daemon.memory.store import ConversationStore


# -- Memory validation tests --

def test_memory_update_rejects_catastrophic_loss(tmp_path: Path):
    dm = DurableMemory(tmp_path / "memory")
    # Write a substantial memory
    dm.update_memory("A" * 1000, validate=False)
    assert len(dm.read_memory()) == 1000

    # Try to replace with something much smaller (< 30%)
    result = dm.update_memory("tiny", validate=True)
    assert result is False
    # Original preserved
    assert len(dm.read_memory()) == 1000


def test_memory_update_rejects_empty(tmp_path: Path):
    dm = DurableMemory(tmp_path / "memory")
    dm.update_memory("Original content", validate=False)

    result = dm.update_memory("", validate=True)
    assert result is False
    assert dm.read_memory() == "Original content"


def test_memory_update_allows_normal_update(tmp_path: Path):
    dm = DurableMemory(tmp_path / "memory")
    dm.update_memory("Original content here", validate=False)

    result = dm.update_memory("Updated content here plus more stuff", validate=True)
    assert result is True
    assert "Updated content" in dm.read_memory()


def test_memory_update_logs_diff(tmp_path: Path):
    dm = DurableMemory(tmp_path / "memory")
    dm.update_memory("Line one\nLine two\nLine three\n", validate=False)

    dm.update_memory("Line one\nLine two modified\nLine three\nLine four\n", validate=True)

    # Check daily log has diff info
    log_content = dm.read_daily_log()
    assert "MEMORY.md updated" in log_content


def test_memory_update_first_write(tmp_path: Path):
    dm = DurableMemory(tmp_path / "memory")
    # First write with no existing memory
    result = dm.update_memory("Brand new memory", validate=True)
    assert result is True
    assert dm.read_memory() == "Brand new memory"


# -- FTS5 search tests --

def test_fts_search(tmp_path: Path):
    store = ConversationStore(tmp_path / "test.db")
    conv = store.get_or_create_conversation(None, "test", "user1")
    store.add_message(conv["id"], "user", "How do I deploy to production?")
    store.add_message(conv["id"], "assistant", "Use the deploy pipeline in CI/CD.")
    store.add_message(conv["id"], "user", "What about database migrations?")
    store.add_message(conv["id"], "assistant", "Run alembic upgrade head before deploying.")

    # Search for deploy-related messages
    results = store.search_conversations("deploy")
    assert len(results) >= 2

    # Search for something specific
    results = store.search_conversations("alembic")
    assert len(results) == 1
    assert "alembic" in results[0]["content"]

    store.close()


def test_fts_search_no_results(tmp_path: Path):
    store = ConversationStore(tmp_path / "test.db")
    conv = store.get_or_create_conversation(None, "test", "user1")
    store.add_message(conv["id"], "user", "Hello world")

    results = store.search_conversations("nonexistent_term_xyz")
    assert results == []
    store.close()


# -- Agent metrics tests --

def test_record_and_query_metrics(tmp_path: Path):
    store = ConversationStore(tmp_path / "test.db")

    store.record_agent_metric(
        agent_name="albert", metric_type="message",
        input_tokens=500, output_tokens=200, cost_usd=0.05,
        duration_ms=3000, model="opus", platform="telegram",
    )
    store.record_agent_metric(
        agent_name="albert", metric_type="heartbeat",
        input_tokens=100, output_tokens=50, cost_usd=0.001,
        duration_ms=1000, model="haiku", platform="scheduler",
    )
    store.record_agent_metric(
        agent_name="penny", metric_type="message",
        input_tokens=200, output_tokens=100, cost_usd=0.02,
        duration_ms=2000, model="sonnet", platform="telegram",
    )

    # Query all agents
    all_metrics = store.get_agent_metrics()
    assert len(all_metrics) == 2  # albert and penny
    names = {m["agent_name"] for m in all_metrics}
    assert names == {"albert", "penny"}

    # Query specific agent
    albert_metrics = store.get_agent_metrics(agent_name="albert")
    assert len(albert_metrics) == 2  # message + heartbeat
    types = {m["metric_type"] for m in albert_metrics}
    assert types == {"message", "heartbeat"}

    store.close()
