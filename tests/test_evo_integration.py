"""Tests for evo integration — [OPTIMIZE] tag, workflow, and config."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_daemon.agents.orchestrator import OPTIMIZE_PATTERN
from claude_daemon.core.config import DaemonConfig


# ------------------------------------------------------------------ #
# [OPTIMIZE] regex pattern tests
# ------------------------------------------------------------------ #


def test_optimize_pattern_basic():
    text = "[OPTIMIZE:albert] Reduce CI pipeline from 45s to under 30s"
    matches = OPTIMIZE_PATTERN.findall(text)
    assert len(matches) == 1
    assert matches[0][0] == "albert"
    assert "CI pipeline" in matches[0][1]


def test_optimize_pattern_multiline():
    text = (
        "[OPTIMIZE:albert] Reduce test suite runtime.\n"
        "Focus on the slowest 5 tests in tests/integration/."
    )
    matches = OPTIMIZE_PATTERN.findall(text)
    assert len(matches) == 1
    assert matches[0][0] == "albert"
    assert "slowest 5 tests" in matches[0][1]


def test_optimize_pattern_multiple():
    text = (
        "[OPTIMIZE:albert] Fix flaky test_auth test\n"
        "[OPTIMIZE:luna] Optimize CSS bundle size"
    )
    matches = OPTIMIZE_PATTERN.findall(text)
    assert len(matches) == 2
    assert matches[0][0] == "albert"
    assert matches[1][0] == "luna"


def test_optimize_pattern_mixed_with_other_tags():
    text = (
        "[DELEGATE:luna] Build the login page\n"
        "[OPTIMIZE:albert] Reduce API response time\n"
        "[HELP:penny] What's the budget?"
    )
    matches = OPTIMIZE_PATTERN.findall(text)
    assert len(matches) == 1
    assert matches[0][0] == "albert"
    assert "API response" in matches[0][1]


def test_optimize_pattern_no_match():
    text = "Just a normal message with no tags"
    matches = OPTIMIZE_PATTERN.findall(text)
    assert len(matches) == 0


def test_optimize_does_not_capture_delegate():
    """Ensure OPTIMIZE pattern doesn't accidentally match DELEGATE tags."""
    from claude_daemon.agents.orchestrator import DELEGATION_PATTERN
    text = "[DELEGATE:albert] Build the auth module [OPTIMIZE:max] Review quality"
    delegates = DELEGATION_PATTERN.findall(text)
    optimizes = OPTIMIZE_PATTERN.findall(text)
    assert len(delegates) == 1
    assert delegates[0][0] == "albert"
    assert len(optimizes) == 1
    assert optimizes[0][0] == "max"


# ------------------------------------------------------------------ #
# Config tests
# ------------------------------------------------------------------ #


def test_evo_config_defaults():
    config = DaemonConfig()
    assert config.evo_enabled is True
    assert config.evo_max_variants == 3
    assert config.evo_max_budget == 2.00


def test_evo_config_custom():
    config = DaemonConfig(evo_enabled=False, evo_max_variants=5, evo_max_budget=5.00)
    assert config.evo_enabled is False
    assert config.evo_max_variants == 5
    assert config.evo_max_budget == 5.00


# ------------------------------------------------------------------ #
# WorkflowEngine.execute_optimization tests
# ------------------------------------------------------------------ #


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch.send_to_agent = AsyncMock()
    return orch


@pytest.fixture
def mock_registry(tmp_path):
    from claude_daemon.agents.agent import Agent
    from claude_daemon.agents.registry import AgentRegistry

    agents_dir = tmp_path / "agents"
    registry = AgentRegistry(agents_dir)
    registry.create_agent("albert", role="CIO")
    return registry


@pytest.fixture
def workflow_engine(mock_orchestrator, mock_registry):
    from claude_daemon.agents.workflow import WorkflowEngine
    return WorkflowEngine(mock_orchestrator, mock_registry)


@pytest.mark.asyncio
async def test_execute_optimization_basic(workflow_engine, mock_orchestrator):
    from claude_daemon.core.process import ClaudeResponse

    mock_orchestrator.send_to_agent.return_value = ClaudeResponse(
        result="Optimization complete: reduced test time from 45s to 28s.",
        session_id="test-session",
        cost=0.15,
        input_tokens=1000,
        output_tokens=500,
        num_turns=1,
        duration_ms=5000,
        is_error=False,
    )

    result = await workflow_engine.execute_optimization(
        agent_name="albert",
        target="Reduce test suite runtime",
    )

    assert result.success is True
    assert len(result.steps) == 1
    assert result.steps[0].agent_name == "albert"
    assert result.steps[0].label == "evo-optimization"
    assert "reduced test time" in result.final_result

    # Verify the prompt was structured correctly
    call_args = mock_orchestrator.send_to_agent.call_args
    assert call_args.kwargs["platform"] == "optimization"
    prompt = call_args.kwargs["prompt"]
    assert "Reduce test suite runtime" in prompt
    assert "evo" in prompt.lower()


@pytest.mark.asyncio
async def test_execute_optimization_agent_not_found(workflow_engine):
    result = await workflow_engine.execute_optimization(
        agent_name="nonexistent",
        target="Optimize something",
    )

    assert result.success is False
    assert len(result.steps) == 1
    assert result.steps[0].is_error is True
    assert "not found" in result.steps[0].result


@pytest.mark.asyncio
async def test_execute_optimization_error(workflow_engine, mock_orchestrator):
    from claude_daemon.core.process import ClaudeResponse

    mock_orchestrator.send_to_agent.return_value = ClaudeResponse(
        result="Error: evo not available",
        session_id="test-session",
        cost=0.01,
        input_tokens=100,
        output_tokens=50,
        num_turns=1,
        duration_ms=1000,
        is_error=True,
    )

    result = await workflow_engine.execute_optimization(
        agent_name="albert",
        target="Fix flaky tests",
    )

    assert result.success is False


# ------------------------------------------------------------------ #
# Bootstrap SOUL.md evo guidance tests
# ------------------------------------------------------------------ #


def test_albert_soul_has_evo_guidance(tmp_path):
    from claude_daemon.agents.bootstrap import create_csuite_workspaces

    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    albert_soul = (agents_dir / "albert" / "SOUL.md").read_text()
    assert "Code Optimization" in albert_soul or "Evo" in albert_soul
    assert "[OPTIMIZE" in albert_soul


def test_max_soul_has_evo_guidance(tmp_path):
    from claude_daemon.agents.bootstrap import create_csuite_workspaces

    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    max_soul = (agents_dir / "max" / "SOUL.md").read_text()
    assert "[OPTIMIZE" in max_soul
