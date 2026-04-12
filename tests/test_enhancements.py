"""Tests for enhancement features: file locking, fuzzy matching, budget, workflow, FTS5, etc."""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_daemon.agents.agent import Agent, AgentIdentity
from claude_daemon.memory.durable import DurableMemory
from claude_daemon.memory.store import ConversationStore


# -- File locking --


def test_memory_file_lock(tmp_path: Path):
    """DurableMemory.update_memory acquires a file lock."""
    mem = DurableMemory(tmp_path / "memory")
    # First write
    assert mem.update_memory("# Memory\nFirst content", validate=False)
    assert "First content" in mem.read_memory()
    # Second write (with validation)
    assert mem.update_memory("# Memory\nSecond content, enough data here")
    assert "Second content" in mem.read_memory()
    # Lock file should exist
    assert (tmp_path / "memory" / ".memory.lock").exists()


def test_memory_rejects_catastrophic_loss_under_lock(tmp_path: Path):
    """File lock doesn't prevent catastrophic loss validation."""
    mem = DurableMemory(tmp_path / "memory")
    mem.update_memory("# Memory\n" + "x" * 1000, validate=False)
    # Try to write much smaller content — should be rejected
    assert not mem.update_memory("tiny")
    assert "x" * 100 in mem.read_memory()  # Original preserved


# -- Fuzzy agent name matching --


def test_fuzzy_agent_matching(tmp_path: Path):
    """Orchestrator resolves close agent name typos."""
    from claude_daemon.agents.orchestrator import Orchestrator
    from claude_daemon.agents.registry import AgentRegistry

    agents_dir = tmp_path / "agents"
    for name in ("johnny", "albert", "luna"):
        ws = agents_dir / name
        ws.mkdir(parents=True)
        (ws / "SOUL.md").write_text(f"# Soul\nI am {name}.")
        (ws / "IDENTITY.md").write_text(f"# Identity\nName: {name}\nRole: test\nModel: sonnet\n")

    registry = AgentRegistry(agents_dir)
    registry.load_all()

    orch = Orchestrator(registry, MagicMock(), MagicMock())

    # Exact match
    agent, msg = orch.resolve_agent("@albert hello")
    assert agent is not None
    assert agent.name == "albert"

    # Fuzzy match (typo)
    agent, msg = orch.resolve_agent("@albet hello")
    assert agent is not None
    assert agent.name == "albert"

    # Fuzzy match for johnny
    agent, msg = orch.resolve_agent("@jony hello")
    assert agent is not None
    assert agent.name == "johnny"

    # Too far from any name — falls through
    agent, msg = orch.resolve_agent("@zzzzz hello")
    assert agent is None


# -- FTS5 query escaping --


def test_fts5_escape_special_chars():
    """FTS5 escape wraps words in quotes to handle special chars."""
    assert ConversationStore._escape_fts5("hello world") == '"hello" "world"'
    assert ConversationStore._escape_fts5("test*") == '"test*"'
    assert ConversationStore._escape_fts5("") == '""'
    # Double quotes are stripped
    assert ConversationStore._escape_fts5('say "hello"') == '"say" "hello"'


# -- Session creation race fix --


def test_session_insert_or_ignore(tmp_path: Path):
    """get_or_create_conversation uses INSERT OR IGNORE for race safety."""
    db_path = tmp_path / "test.db"
    store = ConversationStore(db_path)

    # Create first conversation
    conv1 = store.get_or_create_conversation(
        session_id="sess-001", platform="test", user_id="user1"
    )
    assert conv1["session_id"] == "sess-001"

    # Same session_id again — should return existing, not create duplicate
    conv2 = store.get_or_create_conversation(
        session_id="sess-001", platform="test", user_id="user1"
    )
    assert conv2["id"] == conv1["id"]

    store.close()


# -- Per-agent daily budget --


def test_agent_budget_check():
    """Orchestrator._check_agent_budget returns False when over budget."""
    from claude_daemon.agents.orchestrator import Orchestrator

    mock_pm = MagicMock()
    mock_pm.config = MagicMock()
    mock_pm.config.per_agent_daily_budget = 1.00  # $1/day

    mock_store = MagicMock()
    # Agent has spent $1.50 today
    mock_store.get_agent_metrics.return_value = [{"total_cost": 1.50}]

    orch = Orchestrator(MagicMock(), mock_pm, mock_store)

    assert not orch._check_agent_budget("albert")
    mock_store.get_agent_metrics.assert_called_with(agent_name="albert", days=1)


def test_agent_budget_unlimited():
    """Budget check passes when per_agent_daily_budget is 0 (unlimited)."""
    from claude_daemon.agents.orchestrator import Orchestrator

    mock_pm = MagicMock()
    mock_pm.config = MagicMock()
    mock_pm.config.per_agent_daily_budget = 0.0

    orch = Orchestrator(MagicMock(), mock_pm, MagicMock())

    assert orch._check_agent_budget("albert")


# -- Workflow cost cap --


def test_workflow_result_over_budget():
    """WorkflowResult.is_over_budget detects cost cap breach."""
    from claude_daemon.agents.workflow import WorkflowResult, StepResult

    result = WorkflowResult(max_total_cost=5.00)
    result.steps = [
        StepResult(agent_name="albert", label="build", result="done", cost=3.00),
        StepResult(agent_name="luna", label="ui", result="done", cost=2.50),
    ]
    assert result.is_over_budget()
    assert result.total_cost == 5.50


def test_workflow_result_within_budget():
    """WorkflowResult.is_over_budget returns False when under cap."""
    from claude_daemon.agents.workflow import WorkflowResult, StepResult

    result = WorkflowResult(max_total_cost=10.00)
    result.steps = [
        StepResult(agent_name="albert", label="build", result="done", cost=3.00),
    ]
    assert not result.is_over_budget()


def test_workflow_result_unlimited():
    """WorkflowResult.is_over_budget returns False when max_total_cost is 0."""
    from claude_daemon.agents.workflow import WorkflowResult, StepResult

    result = WorkflowResult(max_total_cost=0.0)
    result.steps = [
        StepResult(agent_name="albert", label="build", result="done", cost=100.0),
    ]
    assert not result.is_over_budget()


# -- Workflow summary includes duration --


def test_workflow_summary_shows_duration():
    """WorkflowResult.summary includes step duration when present."""
    from claude_daemon.agents.workflow import WorkflowResult, StepResult

    result = WorkflowResult()
    result.steps = [
        StepResult(agent_name="albert", label="build", result="done", cost=1.0, duration_ms=5000),
    ]
    summary = result.summary()
    assert "5000ms" in summary
    assert "$1.0000" in summary


# -- Persistent circuit breaker --


def test_circuit_breaker_persistence(tmp_path: Path):
    """Circuit breaker state persists to disk and loads on init."""
    from claude_daemon.scheduler.engine import SchedulerEngine

    config = MagicMock()
    config.data_dir = tmp_path

    # Write some failure state
    state = {"albert:haiku": 3, "luna:sonnet": 1}
    (tmp_path / ".circuit_breaker.json").write_text(json.dumps(state))

    daemon = MagicMock()
    engine = SchedulerEngine(config, daemon)
    assert engine._failure_counts == state

    # Modify and save
    engine._failure_counts["max:opus"] = 2
    engine._save_failure_counts()

    # Verify it was written
    loaded = json.loads((tmp_path / ".circuit_breaker.json").read_text())
    assert loaded["max:opus"] == 2
    assert loaded["albert:haiku"] == 3


# -- Config per_agent_daily_budget --


def test_config_per_agent_daily_budget():
    """Config loads per_agent_daily_budget from YAML."""
    from claude_daemon.core.config import DaemonConfig

    config = DaemonConfig()
    assert config.per_agent_daily_budget == 0.0  # Default: unlimited


# -- Workflow step timeout field --


def test_workflow_step_timeout_default():
    """WorkflowStep has a default timeout of 600s."""
    from claude_daemon.agents.workflow import WorkflowStep

    step = WorkflowStep(agent_name="albert", prompt_template="build it")
    assert step.timeout == 600
