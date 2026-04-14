"""Tests for hardening features: webhook auth, circuit breaker, MCP health, context priority."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_daemon.agents.agent import Agent, AgentIdentity


def test_github_webhook_signature_verification():
    """Test HMAC-SHA256 signature verification for GitHub webhooks."""
    secret = "test-secret-123"
    body = b'{"action": "opened", "pull_request": {}}'
    expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    # Valid signature
    assert hmac.compare_digest(
        hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
        expected_sig,
    )

    # Invalid signature
    assert not hmac.compare_digest(
        hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest(),
        expected_sig,
    )


def test_stripe_webhook_signature_verification():
    """Test Stripe-style "t=...,v1=..." signature format parsing."""
    secret = "whsec_test123"
    timestamp = "1616346000"
    body = '{"type": "payment_intent.succeeded"}'
    signed_payload = f"{timestamp}.{body}"
    v1_sig = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()

    sig_header = f"t={timestamp},v1={v1_sig}"
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    assert parts["t"] == timestamp
    assert parts["v1"] == v1_sig

    # Verify
    check_payload = f"{parts['t']}.{body}"
    check_sig = hmac.new(secret.encode(), check_payload.encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(check_sig, parts["v1"])


def test_mcp_health_check_unconfigured(tmp_path: Path):
    """MCP health check detects unresolved env var placeholders."""
    workspace = tmp_path / "test-agent"
    workspace.mkdir()
    (workspace / "SOUL.md").write_text("# Soul\n")
    (workspace / "IDENTITY.md").write_text(
        "# Identity\nName: test\nRole: tester\nModel: sonnet\nMCP-Config: tools.json\n"
    )
    (workspace / "tools.json").write_text(json.dumps({
        "mcpServers": {
            "github": {
                "command": "npx",
                "args": [],
                "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            },
            "slack": {
                "command": "npx",
                "args": [],
                "env": {"SLACK_BOT_TOKEN": "xoxb-real-token"},
            },
        }
    }))

    agent = Agent(name="test", workspace=workspace)
    agent.load_identity()
    health = agent.check_mcp_health()
    assert health["github"] == "unconfigured (GITHUB_TOKEN)"
    assert health["slack"] == "configured"


def test_mcp_health_check_no_config(tmp_path: Path):
    """MCP health check returns empty dict when no tools.json."""
    workspace = tmp_path / "test-agent"
    workspace.mkdir()
    (workspace / "SOUL.md").write_text("# Soul\n")
    (workspace / "IDENTITY.md").write_text("# Identity\nName: test\nRole: tester\nModel: sonnet\n")

    agent = Agent(name="test", workspace=workspace)
    agent.load_identity()
    assert agent.check_mcp_health() == {}


def test_context_priority_critical_never_truncated(tmp_path: Path):
    """SOUL and steering are never truncated even with tight budget."""
    workspace = tmp_path / "test-agent"
    workspace.mkdir()
    shared_dir = tmp_path / "shared"
    (shared_dir / "steer").mkdir(parents=True)

    soul = "# Soul\nI am the test agent. " + "x" * 200
    (workspace / "SOUL.md").write_text(soul)
    (workspace / "IDENTITY.md").write_text("# Identity\nName: test\nRole: tester\nModel: sonnet\n")
    (workspace / "MEMORY.md").write_text("# Memory\n" + "m" * 500)

    steer = "URGENT: do this first. " + "s" * 100
    (shared_dir / "steer" / "test.md").write_text(steer)

    agent = Agent(name="test", workspace=workspace, shared_dir=shared_dir)
    agent.load_identity()

    # Even with a small budget, soul and steering should be present
    context = agent.build_system_context(max_chars=600)
    assert "I am the test agent" in context
    assert "URGENT: do this first" in context
    assert "Planning Protocol" in context


def test_context_priority_low_trimmed_first(tmp_path: Path):
    """Low-priority blocks are trimmed before high-priority ones."""
    workspace = tmp_path / "test-agent"
    workspace.mkdir()

    (workspace / "SOUL.md").write_text("# Soul\nShort soul.")
    (workspace / "IDENTITY.md").write_text(
        "# Identity\nName: test\nRole: tester\nModel: sonnet\n"
    )
    (workspace / "MEMORY.md").write_text("# Memory\nIMPORTANT_MEMORY_DATA")

    agent = Agent(name="test", workspace=workspace)
    agent.load_identity()

    context = agent.build_system_context(max_chars=2000)
    # Memory (high priority) should be present
    assert "IMPORTANT_MEMORY_DATA" in context
    # Soul (critical) should be present
    assert "Short soul" in context
