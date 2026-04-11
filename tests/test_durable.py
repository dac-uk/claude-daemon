"""Tests for DurableMemory including SOUL.md and REFLECTIONS.md."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from claude_daemon.memory.durable import DurableMemory


@pytest.fixture
def durable(tmp_path: Path) -> DurableMemory:
    return DurableMemory(tmp_path / "memory")


def test_soul_default(durable: DurableMemory):
    """Test that default SOUL.md is returned when file doesn't exist."""
    soul = durable.read_soul()
    assert "Claude Daemon" in soul
    assert "Identity" in soul


def test_ensure_soul(durable: DurableMemory):
    """Test that ensure_soul creates the file."""
    durable.ensure_soul()
    assert durable.soul_file.exists()
    soul = durable.read_soul()
    assert "Claude Daemon" in soul


def test_memory_read_write(durable: DurableMemory):
    durable.update_memory("# My Memory\nI remember things.")
    assert "I remember things" in durable.read_memory()


def test_memory_archive(durable: DurableMemory):
    durable.update_memory("Original memory")
    durable.archive_memory()

    archives = list((durable.memory_dir / "archive").glob("MEMORY_*.md"))
    assert len(archives) == 1
    assert archives[0].read_text() == "Original memory"


def test_reflections(durable: DurableMemory):
    durable.update_reflections("- I should be more concise")
    assert "concise" in durable.read_reflections()


def test_daily_log(durable: DurableMemory):
    durable.append_daily_log("Test entry")
    log = durable.read_daily_log()
    assert "Test entry" in log
    assert date.today().isoformat() in log


def test_recent_logs(durable: DurableMemory):
    today = date.today()
    durable.append_daily_log("Today", today)
    durable.append_daily_log("Yesterday", today - timedelta(days=1))
    durable.append_daily_log("2 days ago", today - timedelta(days=2))

    recent = durable.read_recent_logs(days=3)
    assert "Today" in recent
    assert "Yesterday" in recent
    assert "2 days ago" in recent


def test_cleanup_old_logs(durable: DurableMemory):
    today = date.today()
    durable.append_daily_log("Old", today - timedelta(days=40))
    durable.append_daily_log("Recent", today - timedelta(days=5))
    durable.append_daily_log("Today", today)

    deleted = durable.cleanup_old_logs(retention_days=30)
    assert deleted == 1
    assert durable.read_daily_log(today - timedelta(days=5)) != ""
    assert durable.read_daily_log(today - timedelta(days=40)) == ""


def test_context_block(durable: DurableMemory):
    durable.ensure_soul()
    durable.update_memory("User prefers Python.")
    durable.update_reflections("Be more proactive.")
    durable.append_daily_log("Discussed web scraping.")

    context = durable.get_context_block()
    assert "Identity" in context
    assert "Python" in context
    assert "proactive" in context
    assert "web scraping" in context
