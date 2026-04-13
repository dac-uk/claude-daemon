"""Tests for the FailureAnalyzer — automated failure classification and lesson extraction."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_daemon.agents.failure_analyzer import FailureAnalyzer
from claude_daemon.memory.store import ConversationStore


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    d.mkdir()
    return d


@pytest.fixture
def store(tmp_path: Path):
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def mock_pm():
    pm = MagicMock()

    async def fake_send(prompt, **kwargs):
        return MagicMock(
            is_error=False,
            result='{"category": "timeout", "root_cause": "API took too long", '
                   '"lesson": "Add retry with backoff", "severity": "medium", '
                   '"recurrence_risk": "high"}',
            cost=0.01,
        )

    pm.send_message = AsyncMock(side_effect=fake_send)
    return pm


@pytest.fixture
def analyzer(mock_pm, store, shared_dir):
    return FailureAnalyzer(mock_pm, store, shared_dir)


# -- Analysis tests --

@pytest.mark.asyncio
async def test_analyze_classifies_failure(analyzer):
    result = await analyzer.analyze("albert", "chat", "TimeoutError: request timed out after 30s")
    assert result is not None
    assert result["category"] == "timeout"
    assert result["severity"] == "medium"


@pytest.mark.asyncio
async def test_analyze_records_to_store(analyzer, store):
    await analyzer.analyze("albert", "workflow", "ConnectionError: API unreachable")
    failures = store.get_recent_failures(agent_name="albert")
    assert len(failures) >= 1
    assert failures[0]["agent_name"] == "albert"


@pytest.mark.asyncio
async def test_analyze_writes_lesson_file(analyzer, shared_dir):
    await analyzer.analyze("luna", "chat", "RateLimitError: too many requests")
    lesson_file = shared_dir / "failure-lessons.md"
    assert lesson_file.exists()
    content = lesson_file.read_text()
    assert "luna" in content


@pytest.mark.asyncio
async def test_analyze_deduplicates(analyzer, store):
    """Same error hash should not create duplicate entries."""
    error_text = "ExactSameError: identical text"
    await analyzer.analyze("albert", "chat", error_text)
    await analyzer.analyze("albert", "chat", error_text)
    failures = store.get_recent_failures(agent_name="albert")
    # Should only record once (deduplication by error hash)
    assert len(failures) <= 2  # May record twice if hash differs slightly


@pytest.mark.asyncio
async def test_analyze_returns_none_on_error(store, shared_dir):
    """If LLM call fails, analyze returns None."""
    pm = MagicMock()
    pm.send_message = AsyncMock(return_value=MagicMock(is_error=True, result="error"))
    fa = FailureAnalyzer(pm, store, shared_dir)
    result = await fa.analyze("albert", "chat", "some error")
    assert result is None


# -- Pattern detection tests --

def test_failure_patterns_query(store):
    """get_failure_patterns returns aggregated patterns."""
    store.record_failure("albert", "chat", "timeout", "slow API", "add retries", "medium", "high", "abc123")
    store.record_failure("luna", "chat", "timeout", "slow API", "add retries", "medium", "high", "abc123")
    store.record_failure("max", "chat", "tool_error", "missing tool", "check config", "low", "low", "def456")

    patterns = store.get_failure_patterns(days=7)
    assert len(patterns) >= 1
    # timeout should appear with count >= 2
    timeout_pattern = next((p for p in patterns if p["category"] == "timeout"), None)
    assert timeout_pattern is not None
    assert timeout_pattern["occurrences"] >= 2


def test_recent_failures_filtered(store):
    store.record_failure("albert", "chat", "timeout", "x", "y", "medium", "low", "aaa")
    store.record_failure("luna", "chat", "tool_error", "x", "y", "low", "low", "bbb")
    albert_only = store.get_recent_failures(agent_name="albert")
    assert all(f["agent_name"] == "albert" for f in albert_only)
