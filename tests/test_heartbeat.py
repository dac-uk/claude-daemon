"""Tests for agent heartbeat parsing and MCP config."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_daemon.agents.agent import Agent, AgentIdentity, HeartbeatTask
from claude_daemon.agents.bootstrap import create_csuite_workspaces


def test_heartbeat_parsing(tmp_path: Path):
    ws = tmp_path / "test-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()
    (ws / "HEARTBEAT.md").write_text(
        "# Heartbeat Tasks\n\n"
        "## Morning Briefing\n"
        "Cron: 0 9 * * *\n"
        "Model: sonnet\n"
        "Check email and calendar. Send summary to Slack.\n\n"
        "## Weekly Report\n"
        "Cron: 0 8 * * 1\n"
        "Model: haiku\n"
        "Compile weekly cost report.\n"
        "Include spend by agent.\n"
    )

    agent = Agent(name="test", workspace=ws)
    tasks = agent.parse_heartbeat_tasks()

    assert len(tasks) == 2
    assert tasks[0].title == "Morning Briefing"
    assert tasks[0].cron == "0 9 * * *"
    assert tasks[0].model == "sonnet"
    assert "email and calendar" in tasks[0].prompt

    assert tasks[1].title == "Weekly Report"
    assert tasks[1].cron == "0 8 * * 1"
    assert tasks[1].model == "haiku"
    assert "spend by agent" in tasks[1].prompt


def test_heartbeat_empty(tmp_path: Path):
    ws = tmp_path / "empty-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()

    agent = Agent(name="empty", workspace=ws)
    tasks = agent.parse_heartbeat_tasks()
    assert tasks == []


def test_heartbeat_malformed(tmp_path: Path):
    ws = tmp_path / "bad-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()
    (ws / "HEARTBEAT.md").write_text(
        "# Heartbeat\n\n"
        "## No Cron Task\n"
        "This task has no cron line.\n\n"
        "## Valid Task\n"
        "Cron: 0 12 * * *\n"
        "Do something useful.\n"
    )

    agent = Agent(name="bad", workspace=ws)
    tasks = agent.parse_heartbeat_tasks()
    assert len(tasks) == 1
    assert tasks[0].title == "Valid Task"


def test_mcp_config_from_identity(tmp_path: Path):
    ws = tmp_path / "mcp-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()
    (ws / "IDENTITY.md").write_text(
        "Name: albert\nRole: CIO\nEmoji: 🧠\n"
        "Model: opus\nMCP-Config: tools.json\n"
    )
    (ws / "tools.json").write_text('{"mcpServers": {}}')

    agent = Agent(name="albert", workspace=ws)
    assert agent.identity.mcp_config == "tools.json"
    assert agent.mcp_config_path is not None
    assert agent.mcp_config_path.endswith("tools.json")


def test_mcp_config_missing_file(tmp_path: Path):
    ws = tmp_path / "no-tools"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()
    (ws / "IDENTITY.md").write_text(
        "Name: test\nRole: Test\nMCP-Config: tools.json\n"
    )
    # tools.json does NOT exist

    agent = Agent(name="test", workspace=ws)
    assert agent.identity.mcp_config == "tools.json"
    assert agent.mcp_config_path is None  # File doesn't exist


def test_mcp_config_not_set(tmp_path: Path):
    ws = tmp_path / "vanilla"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()

    agent = Agent(name="vanilla", workspace=ws)
    assert agent.identity.mcp_config == ""
    assert agent.mcp_config_path is None


def test_bootstrap_creates_tools_json(tmp_path: Path):
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    # Albert should have tools.json with github and supabase
    albert_tools = agents_dir / "albert" / "tools.json"
    assert albert_tools.exists()
    import json
    config = json.loads(albert_tools.read_text())
    assert "mcpServers" in config
    assert "github" in config["mcpServers"]
    assert "supabase" in config["mcpServers"]

    # Johnny should have slack, gmail, google-calendar
    johnny_tools = agents_dir / "johnny" / "tools.json"
    assert johnny_tools.exists()
    config = json.loads(johnny_tools.read_text())
    assert "slack" in config["mcpServers"]
    assert "gmail" in config["mcpServers"]
    assert "google-calendar" in config["mcpServers"]


def test_bootstrap_creates_heartbeat(tmp_path: Path):
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    # Penny should have heartbeat with cost audit
    penny_hb = agents_dir / "penny" / "HEARTBEAT.md"
    assert penny_hb.exists()
    content = penny_hb.read_text()
    assert "Daily Cost Audit" in content
    assert "Weekly Financial Report" in content

    # Jeremy should have security scan
    jeremy_hb = agents_dir / "jeremy" / "HEARTBEAT.md"
    assert jeremy_hb.exists()
    assert "Security Scan" in jeremy_hb.read_text()


def test_bootstrap_identity_has_mcp_config(tmp_path: Path):
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    albert_id = (agents_dir / "albert" / "IDENTITY.md").read_text()
    assert "MCP-Config: tools.json" in albert_id

    # Load as agent and verify it parses
    agent = Agent(
        name="albert",
        workspace=agents_dir / "albert",
    )
    assert agent.identity.mcp_config == "tools.json"
    assert agent.mcp_config_path is not None
