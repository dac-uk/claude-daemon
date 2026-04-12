"""Tests for Managed Agents integration — backend routing, config, and fallback."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from claude_daemon.core.process import ClaudeResponse, ProcessManager


# ---------------------------------------------------------------------------
# Config fields
# ---------------------------------------------------------------------------

def test_config_managed_agents_defaults():
    from claude_daemon.core.config import DaemonConfig
    config = DaemonConfig()
    assert config.managed_agents_enabled is False
    assert "planning" in config.managed_agents_task_types
    assert "workflow" in config.managed_agents_task_types
    assert "rem_sleep" in config.managed_agents_task_types
    assert "improvement" in config.managed_agents_task_types


def test_config_managed_agents_from_yaml(tmp_path):
    from claude_daemon.core.config import DaemonConfig

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "claude": {
            "managed_agents_enabled": True,
            "managed_agents_task_types": ["planning", "workflow"],
        },
    }))
    config = DaemonConfig.load(cfg_file)
    assert config.managed_agents_enabled is True
    assert config.managed_agents_task_types == ["planning", "workflow"]


def test_config_managed_agents_disabled_by_default_from_yaml(tmp_path):
    from claude_daemon.core.config import DaemonConfig

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"claude": {}}))
    config = DaemonConfig.load(cfg_file)
    assert config.managed_agents_enabled is False


# ---------------------------------------------------------------------------
# Backend routing logic
# ---------------------------------------------------------------------------

def test_should_use_managed_disabled():
    """When managed_agents_enabled is False, never routes to managed."""
    config = MagicMock()
    config.managed_agents_enabled = False
    config.managed_agents_task_types = ["planning"]
    config.max_concurrent_sessions = 3

    pm = ProcessManager(config)
    # Even if we force a managed backend to exist
    pm._managed = MagicMock()
    assert pm._should_use_managed("planning", "albert") is False


def test_should_use_managed_no_api_key():
    """Without ANTHROPIC_API_KEY, managed property returns None."""
    config = MagicMock()
    config.managed_agents_enabled = True
    config.managed_agents_task_types = ["planning"]
    config.max_concurrent_sessions = 3

    pm = ProcessManager(config)
    with patch.dict(os.environ, {}, clear=True):
        assert pm.managed is None
        assert pm._should_use_managed("planning", "albert") is False


def test_should_use_managed_enabled_matching_task_type():
    """When enabled + task_type matches, should route to managed."""
    config = MagicMock()
    config.managed_agents_enabled = True
    config.managed_agents_task_types = ["planning", "workflow"]
    config.max_concurrent_sessions = 3

    pm = ProcessManager(config)
    pm._managed = MagicMock()  # Simulate available backend
    assert pm._should_use_managed("planning", "albert") is True
    assert pm._should_use_managed("workflow", "albert") is True


def test_should_use_managed_non_matching_task_type():
    """Chat and heartbeat should NOT route to managed even when enabled."""
    config = MagicMock()
    config.managed_agents_enabled = True
    config.managed_agents_task_types = ["planning", "workflow"]
    config.max_concurrent_sessions = 3

    pm = ProcessManager(config)
    pm._managed = MagicMock()
    assert pm._should_use_managed("chat", "albert") is False
    assert pm._should_use_managed("heartbeat", "johnny") is False
    assert pm._should_use_managed("default", "luna") is False
    assert pm._should_use_managed("scheduled", "penny") is False


# ---------------------------------------------------------------------------
# Fallback behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_message_fallback_on_managed_error():
    """If managed backend fails, should fall back to CLI execution."""
    config = MagicMock()
    config.managed_agents_enabled = True
    config.managed_agents_task_types = ["planning"]
    config.max_concurrent_sessions = 3
    config.max_budget_per_message = 0.50
    config.model_max_retries = 0
    config.permission_mode = "auto"
    config.claude_binary = "claude"
    config.default_model = None
    config.mcp_config = None
    config.process_timeout = 10
    config.stream_idle_timeout_ms = 600000
    config.auto_compact_pct = 50

    pm = ProcessManager(config)

    # Set up managed backend that raises
    managed_mock = MagicMock()
    managed_mock.send_message = AsyncMock(side_effect=Exception("API down"))
    pm._managed = managed_mock

    # Set up CLI execution to return a response
    cli_response = ClaudeResponse(
        result="CLI fallback response",
        session_id="test-123",
        cost=0.01, input_tokens=50, output_tokens=25,
        num_turns=1, duration_ms=500, is_error=False,
    )

    with patch.object(pm, '_execute_buffered', new_callable=AsyncMock, return_value=cli_response):
        response = await pm.send_message(
            prompt="Test planning task",
            task_type="planning",
            agent_name="albert",
        )

    assert response.result == "CLI fallback response"
    assert not response.is_error
    # Managed was attempted
    managed_mock.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_send_message_no_fallback_when_cli_task_type():
    """CLI task types should never try managed backend."""
    config = MagicMock()
    config.managed_agents_enabled = True
    config.managed_agents_task_types = ["planning"]
    config.max_concurrent_sessions = 3
    config.max_budget_per_message = 0.50
    config.model_max_retries = 0
    config.permission_mode = "auto"
    config.claude_binary = "claude"
    config.default_model = None
    config.mcp_config = None
    config.process_timeout = 10
    config.stream_idle_timeout_ms = 600000
    config.auto_compact_pct = 50

    pm = ProcessManager(config)

    managed_mock = MagicMock()
    managed_mock.send_message = AsyncMock()
    pm._managed = managed_mock

    cli_response = ClaudeResponse(
        result="CLI response",
        session_id="test-123",
        cost=0.01, input_tokens=50, output_tokens=25,
        num_turns=1, duration_ms=500, is_error=False,
    )

    with patch.object(pm, '_execute_buffered', new_callable=AsyncMock, return_value=cli_response):
        response = await pm.send_message(
            prompt="Quick chat",
            task_type="chat",
            agent_name="johnny",
        )

    assert response.result == "CLI response"
    # Managed was NOT attempted
    managed_mock.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# ManagedAgentBackend unit tests
# ---------------------------------------------------------------------------

def test_managed_backend_status():
    """get_status returns correct structure."""
    from claude_daemon.core.managed_agents import ManagedAgentBackend

    config = MagicMock()
    config.managed_agents_enabled = True
    config.managed_agents_task_types = ["planning", "workflow"]

    backend = ManagedAgentBackend(config)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
        status = backend.get_status()

    assert status["enabled"] is True
    assert status["agent_count"] == 0
    assert status["registered_agents"] == []
    assert status["environment_id"] is None
    assert "planning" in status["task_types"]
    assert status["api_key_set"] is True


def test_managed_backend_cost_estimation():
    """Cost estimation returns reasonable values."""
    from claude_daemon.core.managed_agents import ManagedAgentBackend

    # Opus pricing
    cost = ManagedAgentBackend._estimate_cost(1000, 500, "opus")
    assert cost > 0
    assert cost == (1000 * 15 + 500 * 75) / 1_000_000

    # Sonnet pricing
    cost = ManagedAgentBackend._estimate_cost(1000, 500, "sonnet")
    assert cost == (1000 * 3 + 500 * 15) / 1_000_000

    # Haiku pricing
    cost = ManagedAgentBackend._estimate_cost(1000, 500, "haiku")
    assert cost == (1000 * 0.25 + 500 * 1.25) / 1_000_000


@pytest.mark.asyncio
async def test_managed_backend_send_unregistered_agent():
    """Sending to an unregistered agent returns an error response."""
    from claude_daemon.core.managed_agents import ManagedAgentBackend

    config = MagicMock()
    config.managed_agents_enabled = True

    backend = ManagedAgentBackend(config)
    backend._client = MagicMock()  # Skip real SDK init
    # Don't register any agents

    response = await backend.send_message("test prompt", "nonexistent_agent")
    assert response.is_error
    assert "not registered" in response.result


# ---------------------------------------------------------------------------
# Daemon methods
# ---------------------------------------------------------------------------

def test_daemon_get_managed_agents_status_no_pm():
    """Status works even when process_manager is None (pre-startup)."""
    from claude_daemon.core.daemon import ClaudeDaemon
    from claude_daemon.core.config import DaemonConfig

    config = DaemonConfig()
    daemon = ClaudeDaemon(config)
    # process_manager is None before start()
    status = daemon.get_managed_agents_status()
    assert status["enabled"] is False
    assert status["agent_count"] == 0


# ---------------------------------------------------------------------------
# Orchestrator wiring — task_type and agent_name reach PM
# ---------------------------------------------------------------------------

def test_orchestrator_passes_task_type_and_agent_name():
    """Verify orchestrator.send_to_agent passes task_type and agent_name to PM."""
    import inspect
    from claude_daemon.agents.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator.send_to_agent)
    assert "task_type=task_type" in source
    assert "agent_name=agent.name" in source


def test_orchestrator_stream_passes_task_type_and_agent_name():
    """Verify orchestrator.stream_to_agent passes task_type and agent_name to PM."""
    import inspect
    from claude_daemon.agents.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator.stream_to_agent)
    assert "task_type=task_type" in source
    assert "agent_name=agent.name" in source


# ---------------------------------------------------------------------------
# Memory compaction wiring
# ---------------------------------------------------------------------------

def test_rem_sleep_passes_task_type():
    """Verify REM sleep call includes task_type='rem_sleep'."""
    import inspect
    from claude_daemon.memory.compactor import ContextCompactor

    source = inspect.getsource(ContextCompactor.rem_sleep)
    assert 'task_type="rem_sleep"' in source


# ---------------------------------------------------------------------------
# ProcessManager send_message accepts new params
# ---------------------------------------------------------------------------

def test_send_message_signature_includes_task_type_and_agent_name():
    """ProcessManager.send_message must accept task_type and agent_name."""
    import inspect
    sig = inspect.signature(ProcessManager.send_message)
    assert "task_type" in sig.parameters
    assert "agent_name" in sig.parameters
    # Defaults
    assert sig.parameters["task_type"].default == "default"
    assert sig.parameters["agent_name"].default is None


def test_stream_message_signature_includes_task_type_and_agent_name():
    """ProcessManager.stream_message must accept task_type and agent_name."""
    import inspect
    sig = inspect.signature(ProcessManager.stream_message)
    assert "task_type" in sig.parameters
    assert "agent_name" in sig.parameters
