"""Tests for the self-improvement loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_daemon.agents.agent import Agent
from claude_daemon.agents.bootstrap import create_csuite_workspaces, create_shared_workspace


def test_agents_have_improvement_directives(tmp_path: Path):
    """All C-suite agents should have Continuous Improvement in their SOUL.md."""
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    for name in ["johnny", "albert", "luna", "max", "penny", "jeremy", "sophie"]:
        soul = (agents_dir / name / "SOUL.md").read_text()
        assert "Continuous Improvement" in soul, f"{name} missing improvement directive"


def test_johnny_has_strategy_heartbeats(tmp_path: Path):
    """Johnny should have weekly and monthly improvement heartbeats."""
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    agent = Agent(name="johnny", workspace=agents_dir / "johnny")
    tasks = agent.parse_heartbeat_tasks()

    task_titles = [t.title for t in tasks]
    assert "Weekly Strategy Review" in task_titles
    assert "Monthly Initiative Planning" in task_titles


def test_albert_has_research_heartbeats(tmp_path: Path):
    """Albert should have tech debt and architecture review heartbeats."""
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    agent = Agent(name="albert", workspace=agents_dir / "albert")
    tasks = agent.parse_heartbeat_tasks()

    task_titles = [t.title for t in tasks]
    assert "Weekly Tech Debt Audit" in task_titles
    assert "Fortnightly Architecture Review" in task_titles


def test_max_has_quality_retro(tmp_path: Path):
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    agent = Agent(name="max", workspace=agents_dir / "max")
    tasks = agent.parse_heartbeat_tasks()
    assert any("Quality Retrospective" in t.title for t in tasks)


def test_penny_has_cost_optimisation(tmp_path: Path):
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    agent = Agent(name="penny", workspace=agents_dir / "penny")
    tasks = agent.parse_heartbeat_tasks()
    assert any("Cost Optimisation" in t.title for t in tasks)


def test_playbooks_injected_in_context(tmp_path: Path):
    """Agent context should include shared playbook index."""
    shared = tmp_path / "shared"
    shared.mkdir()
    playbooks = shared / "playbooks"
    playbooks.mkdir()
    (playbooks / "auth-patterns.md").write_text("# Auth Patterns\nUse OAuth2...")
    (playbooks / "deploy-checklist.md").write_text("# Deploy\n1. Run tests...")

    ws = tmp_path / "agent"
    agent = Agent(name="test", workspace=ws, shared_dir=shared)
    ctx = agent.build_system_context()

    assert "auth-patterns" in ctx
    assert "deploy-checklist" in ctx
    assert "Shared Playbooks" in ctx


def test_learnings_injected_in_context(tmp_path: Path):
    """Agent context should include shared learnings."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "learnings.md").write_text(
        "# Team Learnings\n\n"
        "- Always validate input at API boundaries\n"
        "- Use atomic commits for multi-step changes\n"
    )

    ws = tmp_path / "agent"
    agent = Agent(name="test", workspace=ws, shared_dir=shared)
    ctx = agent.build_system_context()

    assert "Team Learnings" in ctx
    assert "atomic commits" in ctx


def test_steering_still_works(tmp_path: Path):
    """Steering directives should still be injected."""
    shared = tmp_path / "shared"
    (shared / "steer").mkdir(parents=True)
    (shared / "steer" / "test.md").write_text("Focus on performance optimisation this week.")

    ws = tmp_path / "agent"
    agent = Agent(name="test", workspace=ws, shared_dir=shared)
    ctx = agent.build_system_context()

    assert "STEERING" in ctx
    assert "performance optimisation" in ctx


def test_improvement_heartbeat_count(tmp_path: Path):
    """Total heartbeat count should be higher now with research tasks."""
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    total = 0
    for name in ["johnny", "albert", "luna", "max", "penny", "jeremy", "sophie"]:
        agent = Agent(name=name, workspace=agents_dir / name)
        total += len(agent.parse_heartbeat_tasks())

    # Should be at least 20 heartbeat tasks across all agents
    assert total >= 20, f"Only {total} heartbeat tasks — expected 20+"
