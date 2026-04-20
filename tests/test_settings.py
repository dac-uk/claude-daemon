"""Tests for agent settings.json generation, effort mapping, and best practice integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_daemon.agents.agent import Agent, AgentIdentity, _EFFORT_BY_TASK_TYPE
from claude_daemon.agents.bootstrap import (
    AGENT_ALLOW_RULES,
    AGENT_DENY_RULES,
    generate_agent_settings,
    refresh_agent_configs,
    create_csuite_workspaces,
)


def test_generate_agent_settings_defaults():
    settings = generate_agent_settings()
    assert settings["alwaysThinkingEnabled"] is True
    assert settings["includeGitInstructions"] is True
    assert settings["permissions"]["allow"] == AGENT_ALLOW_RULES
    assert settings["permissions"]["deny"] == AGENT_DENY_RULES
    assert "Bash(sudo *)" in settings["permissions"]["deny"]
    assert "Bash(*)" in settings["permissions"]["allow"]


def test_generate_agent_settings_custom_deny():
    extra = ["Bash(docker rm *)"]
    settings = generate_agent_settings(deny_rules=extra)
    assert "Bash(docker rm *)" in settings["permissions"]["deny"]
    assert "Bash(sudo *)" in settings["permissions"]["deny"]  # defaults still present


def test_generate_agent_settings_thinking_toggle():
    on = generate_agent_settings(thinking_enabled=True)
    off = generate_agent_settings(thinking_enabled=False)
    assert on["alwaysThinkingEnabled"] is True
    assert off["alwaysThinkingEnabled"] is False


def test_refresh_creates_settings_json(tmp_path):
    for name in ["alice", "bob"]:
        ws = tmp_path / name
        ws.mkdir()
        (ws / "IDENTITY.md").write_text(f"Name: {name}\n")

    with patch.dict(os.environ, {}, clear=True):
        counts = refresh_agent_configs(tmp_path)

    assert "alice" in counts
    assert "bob" in counts

    alice_settings = json.loads((tmp_path / "alice" / "settings.json").read_text())
    assert "permissions" in alice_settings
    assert alice_settings["alwaysThinkingEnabled"] is True
    assert "Bash(sudo *)" in alice_settings["permissions"]["deny"]

    # tools.json also written
    assert (tmp_path / "alice" / "tools.json").exists()


def test_agent_settings_path(tmp_path):
    ws = tmp_path / "test-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()

    agent = Agent(name="test", workspace=ws)
    assert agent.settings_path is None  # no settings.json yet

    (ws / "settings.json").write_text('{"permissions": {}}')
    assert agent.settings_path is not None
    assert agent.settings_path.endswith("settings.json")


def test_agent_get_effort(tmp_path):
    ws = tmp_path / "test-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()

    agent = Agent(name="test", workspace=ws)
    assert agent.get_effort("scheduled") == "low"
    assert agent.get_effort("heartbeat") == "low"
    assert agent.get_effort("chat") == "medium"
    assert agent.get_effort("default") == "medium"
    assert agent.get_effort("planning") == "high"
    assert agent.get_effort("unknown") == "medium"


def test_important_tags_in_context(tmp_path):
    ws = tmp_path / "test-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()
    (ws / "SOUL.md").write_text("# Soul\n\nI am test agent.\n")

    agent = Agent(name="test", workspace=ws)
    ctx = agent.build_system_context()
    assert "<important>" in ctx
    assert "</important>" in ctx
    assert "Planning Protocol" in ctx


def test_planning_protocol_has_verify(tmp_path):
    ws = tmp_path / "test-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()

    agent = Agent(name="test", workspace=ws)
    ctx = agent.build_system_context()
    assert "VERIFY" in ctx
    assert "RESEARCH" in ctx
    assert "REPORT" in ctx


def test_gotchas_in_context(tmp_path):
    ws = tmp_path / "test-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()
    (ws / "GOTCHAS.md").write_text("# Gotchas\n\n- Never do X.\n- Always check Y.\n")

    agent = Agent(name="test", workspace=ws)
    ctx = agent.build_system_context()
    assert "Never do X" in ctx
    assert "Gotchas" in ctx


def test_bootstrap_creates_gotchas(tmp_path):
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    albert_gotchas = agents_dir / "albert" / "GOTCHAS.md"
    assert albert_gotchas.exists()
    assert "tests after code changes" in albert_gotchas.read_text()

    penny_gotchas = agents_dir / "penny" / "GOTCHAS.md"
    assert penny_gotchas.exists()
    assert "Token costs" in penny_gotchas.read_text()


def test_bootstrap_creates_settings_json(tmp_path):
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    for child in agents_dir.iterdir():
        if child.is_dir():
            settings_file = child / "settings.json"
            assert settings_file.exists(), f"{child.name} missing settings.json"
            config = json.loads(settings_file.read_text())
            assert "permissions" in config
            assert config["alwaysThinkingEnabled"] is True


def test_config_new_fields():
    from claude_daemon.core.config import DaemonConfig
    config = DaemonConfig()
    assert config.thinking_enabled is True
    assert config.default_effort == ""
    assert config.auto_compact_pct == 80
    assert config.agent_deny_rules == []


def test_config_loads_new_fields(tmp_path):
    import yaml
    from claude_daemon.core.config import DaemonConfig

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "claude": {
            "thinking_enabled": False,
            "default_effort": "high",
            "auto_compact_pct": 40,
            "agent_deny_rules": ["Bash(docker rm *)"],
        },
    }))

    config = DaemonConfig.load(cfg_file)
    assert config.thinking_enabled is False
    assert config.default_effort == "high"
    assert config.auto_compact_pct == 40
    assert config.agent_deny_rules == ["Bash(docker rm *)"]


def test_build_args_includes_settings_and_effort():
    from claude_daemon.core.process import ProcessManager

    config = MagicMock()
    config.claude_binary = "claude"
    config.permission_mode = "auto"
    config.default_model = None
    config.mcp_config = None
    config.max_concurrent_sessions = 5
    pm = ProcessManager(config)

    args, _ = pm._build_args(
        "hello", None, None, 0.5,
        settings_path="/tmp/settings.json",
        effort="high",
    )
    assert "--settings" in args
    assert "/tmp/settings.json" in args
    assert "--effort" in args
    assert "high" in args


def test_subprocess_env_has_autocompact():
    from claude_daemon.core.process import ProcessManager

    config = MagicMock()
    config.stream_idle_timeout_ms = 600000
    config.auto_compact_pct = 50
    config.max_concurrent_sessions = 5
    pm = ProcessManager(config)

    env = pm._subprocess_env()
    assert env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] == "600000"
    assert env["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "50"


def test_skill_awareness_in_context_for_non_orchestrator(tmp_path):
    ws = tmp_path / "test-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()

    agent = Agent(name="albert", workspace=ws)
    agent.is_orchestrator = False
    ctx = agent.build_system_context()
    assert "Available Capabilities" in ctx


def test_skill_awareness_not_in_orchestrator_context(tmp_path):
    ws = tmp_path / "test-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()

    agent = Agent(name="johnny", workspace=ws)
    agent.is_orchestrator = True
    ctx = agent.build_system_context()
    assert "Available Capabilities" not in ctx
