"""Tests for the MCP server pool — catalog, generation, tier logic, and management."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Catalog & generation
# ---------------------------------------------------------------------------

def test_catalog_has_expected_servers():
    from claude_daemon.agents.bootstrap import MCP_SERVER_CATALOG
    assert "github" in MCP_SERVER_CATALOG
    assert "slack" in MCP_SERVER_CATALOG
    assert "tavily" in MCP_SERVER_CATALOG
    assert "fetch" in MCP_SERVER_CATALOG
    assert "time" in MCP_SERVER_CATALOG
    assert len(MCP_SERVER_CATALOG) >= 36


def test_catalog_entries_have_required_fields():
    from claude_daemon.agents.bootstrap import MCP_SERVER_CATALOG
    for name, tmpl in MCP_SERVER_CATALOG.items():
        assert "command" in tmpl, f"{name} missing 'command'"
        assert "args" in tmpl, f"{name} missing 'args'"
        assert "category" in tmpl, f"{name} missing 'category'"
        assert "description" in tmpl, f"{name} missing 'description'"
        # env may be absent or empty for zero-config servers
        assert isinstance(tmpl.get("env", {}), dict), f"{name} 'env' is not a dict"


def test_server_env_vars_extraction():
    from claude_daemon.agents.bootstrap import _server_env_vars
    tmpl = {"env": {"FOO": "${FOO}", "BAR": "${BAR}"}}
    assert sorted(_server_env_vars(tmpl)) == ["BAR", "FOO"]

    # No env
    assert _server_env_vars({"env": {}}) == []
    assert _server_env_vars({}) == []


def test_generate_mcp_config_includes_zero_config():
    """Zero-config servers (no env vars) should always appear."""
    from claude_daemon.agents.bootstrap import generate_mcp_config
    with patch.dict(os.environ, {}, clear=True):
        cfg = generate_mcp_config()
    servers = cfg["mcpServers"]
    assert "fetch" in servers
    assert "git" in servers
    assert "time" in servers
    assert "memory" in servers
    assert "context7" in servers


def test_generate_mcp_config_excludes_unconfigured():
    """Token-required servers should NOT appear when env var is unset."""
    from claude_daemon.agents.bootstrap import generate_mcp_config
    env = {k: "" for k in os.environ}  # blank everything
    with patch.dict(os.environ, {}, clear=True):
        cfg = generate_mcp_config()
    servers = cfg["mcpServers"]
    assert "tavily" not in servers  # needs TAVILY_API_KEY
    assert "notion" not in servers  # needs NOTION_API_KEY


def test_generate_mcp_config_includes_configured():
    """Token-required servers should appear when env var IS set."""
    from claude_daemon.agents.bootstrap import generate_mcp_config
    with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}, clear=True):
        cfg = generate_mcp_config()
    servers = cfg["mcpServers"]
    assert "tavily" in servers
    # fetch (zero-config) should still be there
    assert "fetch" in servers


def test_generate_mcp_config_respects_disabled():
    """Disabled servers should be excluded even if they are zero-config."""
    from claude_daemon.agents.bootstrap import generate_mcp_config
    with patch.dict(os.environ, {}, clear=True):
        cfg = generate_mcp_config(disabled_servers=["fetch", "git"])
    servers = cfg["mcpServers"]
    assert "fetch" not in servers
    assert "git" not in servers
    # Others still present
    assert "time" in servers


def test_generate_mcp_config_strips_metadata():
    """Generated config entries should not contain category/description."""
    from claude_daemon.agents.bootstrap import generate_mcp_config
    with patch.dict(os.environ, {}, clear=True):
        cfg = generate_mcp_config()
    for name, entry in cfg["mcpServers"].items():
        assert "category" not in entry, f"{name} has 'category' in output"
        assert "description" not in entry, f"{name} has 'description' in output"
        assert "command" in entry
        assert "args" in entry


# ---------------------------------------------------------------------------
# Refresh agent tools.json
# ---------------------------------------------------------------------------

def test_refresh_writes_all_agents(tmp_path):
    from claude_daemon.agents.bootstrap import refresh_agent_tools_json

    # Create two fake agent workspaces
    for name in ["alice", "bob"]:
        ws = tmp_path / name
        ws.mkdir()
        (ws / "IDENTITY.md").write_text(f"Name: {name}\n")

    with patch.dict(os.environ, {}, clear=True):
        counts = refresh_agent_tools_json(tmp_path)

    assert "alice" in counts
    assert "bob" in counts
    assert counts["alice"] == counts["bob"]  # same config for both

    # Verify files were written
    alice_cfg = json.loads((tmp_path / "alice" / "tools.json").read_text())
    assert "mcpServers" in alice_cfg
    assert "fetch" in alice_cfg["mcpServers"]  # zero-config


def test_refresh_skips_non_agent_dirs(tmp_path):
    """Directories without IDENTITY.md should be ignored."""
    from claude_daemon.agents.bootstrap import refresh_agent_tools_json

    (tmp_path / "not-an-agent").mkdir()
    ws = tmp_path / "real-agent"
    ws.mkdir()
    (ws / "IDENTITY.md").write_text("Name: real-agent\n")

    with patch.dict(os.environ, {}, clear=True):
        counts = refresh_agent_tools_json(tmp_path)

    assert "real-agent" in counts
    assert "not-an-agent" not in counts


# ---------------------------------------------------------------------------
# Catalog status
# ---------------------------------------------------------------------------

def test_get_mcp_catalog_status_all_tiers():
    from claude_daemon.agents.bootstrap import get_mcp_catalog_status

    with patch.dict(os.environ, {"TAVILY_API_KEY": "test"}, clear=True):
        statuses = get_mcp_catalog_status(disabled_servers=["git"])

    by_name = {s["name"]: s for s in statuses}

    # fetch = zero-config → active
    assert by_name["fetch"]["tier"] == "zero-config"
    assert by_name["fetch"]["status"] == "active"

    # tavily = configured → active
    assert by_name["tavily"]["tier"] == "configured"
    assert by_name["tavily"]["status"] == "active"

    # notion = needs-token → inactive
    assert by_name["notion"]["tier"] == "needs-token"
    assert by_name["notion"]["status"] == "inactive"

    # git = disabled → disabled
    assert by_name["git"]["tier"] == "disabled"
    assert by_name["git"]["status"] == "disabled"


# ---------------------------------------------------------------------------
# Env var detection
# ---------------------------------------------------------------------------

def test_detect_mcp_server_for_var():
    from claude_daemon.core.env_manager import detect_mcp_server_for_var

    assert detect_mcp_server_for_var("TAVILY_API_KEY") == "tavily"
    assert detect_mcp_server_for_var("GITHUB_TOKEN") == "github"
    assert detect_mcp_server_for_var("NOTION_API_KEY") == "notion"
    assert detect_mcp_server_for_var("NONEXISTENT_KEY") is None
    assert detect_mcp_server_for_var("TELEGRAM_BOT_TOKEN") is None  # daemon var, not MCP


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def test_config_loads_disabled_mcp_servers(tmp_path):
    import yaml
    from claude_daemon.core.config import DaemonConfig

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "claude": {"disabled_mcp_servers": ["snowflake", "bigquery"]},
    }))

    config = DaemonConfig.load(cfg_file)
    assert config.disabled_mcp_servers == ["snowflake", "bigquery"]


def test_config_defaults_disabled_mcp_empty():
    from claude_daemon.core.config import DaemonConfig
    config = DaemonConfig()
    assert config.disabled_mcp_servers == []


# ---------------------------------------------------------------------------
# Bootstrap creates MCP-Config for all agents
# ---------------------------------------------------------------------------

def test_bootstrap_always_sets_mcp_config(tmp_path):
    """All agents should get MCP-Config: tools.json in IDENTITY.md."""
    from claude_daemon.agents.bootstrap import create_csuite_workspaces

    create_csuite_workspaces(tmp_path)

    for child in tmp_path.iterdir():
        if child.is_dir():
            identity = (child / "IDENTITY.md").read_text()
            assert "MCP-Config: tools.json" in identity, (
                f"Agent {child.name} missing MCP-Config line"
            )


# ---------------------------------------------------------------------------
# Backwards-compat alias
# ---------------------------------------------------------------------------

def test_mcp_server_templates_alias():
    from claude_daemon.agents.bootstrap import MCP_SERVER_TEMPLATES, MCP_SERVER_CATALOG
    assert MCP_SERVER_TEMPLATES is MCP_SERVER_CATALOG
