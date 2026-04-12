"""Environment variable manager — list, set, scan, report.

Centralizes all env var operations so CLI, chat, HTTP API, and startup checks
all use the same logic. Never exposes full secret values — only masked forms.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv, set_key

from claude_daemon.utils import paths

log = logging.getLogger(__name__)

# All known environment variables the daemon and its MCP tools may use
KNOWN_ENV_VARS: list[str] = [
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "CLAUDE_DAEMON_API_KEY",
    "GITHUB_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_TEAM_ID",
    "SUPABASE_ACCESS_TOKEN",
    "SUPABASE_PROJECT_REF",
    "GMAIL_OAUTH_CREDENTIALS",
    "GCAL_OAUTH_CREDENTIALS",
    "GITHUB_WEBHOOK_SECRET",
    "STRIPE_WEBHOOK_SECRET",
    "PAPERCLIP_URL",
    "PAPERCLIP_API_KEY",
]

_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def get_env_file_path() -> Path:
    """Return the canonical .env file path."""
    return paths.config_dir() / ".env"


def _mask(value: str) -> str:
    """Mask a secret value, showing only the last 4 characters."""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def list_env_vars() -> list[dict]:
    """List all known env vars with set/unset status and masked values.

    Returns list of {"key": str, "status": "set"|"unset", "masked": str|None}.
    """
    result = []
    for key in KNOWN_ENV_VARS:
        value = os.environ.get(key)
        if value:
            result.append({"key": key, "status": "set", "masked": _mask(value)})
        else:
            result.append({"key": key, "status": "unset", "masked": None})
    return result


def set_env_var(key: str, value: str) -> None:
    """Set an env var in the .env file and the current process environment.

    Uses python-dotenv's set_key() for safe file writes (handles quoting,
    escaping, and preserves existing comments).
    """
    if not _KEY_PATTERN.match(key):
        raise ValueError(f"Invalid env var name: {key}")

    env_path = get_env_file_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure file exists
    if not env_path.exists():
        env_path.write_text("# Claude Daemon environment variables\n")

    set_key(str(env_path), key, value)
    os.environ[key] = value
    log.info("Set env var %s (****%s)", key, value[-4:] if len(value) >= 4 else "****")


def reload_env() -> None:
    """Reload the .env file into os.environ (override existing values)."""
    env_path = get_env_file_path()
    if env_path.exists():
        load_dotenv(env_path, override=True)
        log.debug("Reloaded .env from %s", env_path)


def scan_mcp_unresolved(agent_registry) -> dict[str, list[str]]:
    """Scan all agents' MCP health for unresolved env var placeholders.

    Returns {agent_name: [var_names]} for agents with missing env vars.
    """
    missing: dict[str, list[str]] = {}
    for agent in agent_registry:
        health = agent.check_mcp_health()
        for server_name, status in health.items():
            if server_name == "_error":
                continue
            if "unconfigured" in status:
                # Extract var names from "unconfigured (VAR1, VAR2)"
                vars_match = re.search(r"\((.+)\)", status)
                if vars_match:
                    var_names = [v.strip() for v in vars_match.group(1).split(",")]
                    if agent.name not in missing:
                        missing[agent.name] = []
                    missing[agent.name].extend(var_names)
    return missing


def get_missing_env_report(agent_registry) -> str | None:
    """Generate a human-readable report of missing env vars.

    Returns None if everything is configured.
    """
    mcp_missing = scan_mcp_unresolved(agent_registry)
    if not mcp_missing:
        return None

    # Invert: var_name -> [agent_names]
    var_to_agents: dict[str, list[str]] = {}
    for agent_name, vars_list in mcp_missing.items():
        for var in vars_list:
            if var not in var_to_agents:
                var_to_agents[var] = []
            var_to_agents[var].append(agent_name)

    lines = ["Missing environment variables detected:\n"]
    for var, agents in sorted(var_to_agents.items()):
        agents_str = ", ".join(agents)
        lines.append(f"  - {var} (used by: {agents_str})")

    lines.append("")
    lines.append("Fix via chat:  /setenv VARIABLE_NAME your_value")
    lines.append("Fix via CLI:   claude-daemon env set VARIABLE_NAME=your_value")
    lines.append("Fix via API:   POST /api/config/env {\"key\": \"...\", \"value\": \"...\"}")

    return "\n".join(lines)
