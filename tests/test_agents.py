"""Tests for the multi-agent system."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_daemon.agents.agent import Agent, AgentIdentity
from claude_daemon.agents.bootstrap import create_csuite_workspaces, create_shared_workspace
from claude_daemon.agents.registry import AgentRegistry
from claude_daemon.agents.orchestrator import Orchestrator, AGENT_ADDRESS_PATTERN


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    return tmp_path / "agents"


@pytest.fixture
def registry(agents_dir: Path) -> AgentRegistry:
    reg = AgentRegistry(agents_dir)
    return reg


# -- Agent tests --

def test_agent_creates_workspace(tmp_path: Path):
    ws = tmp_path / "test-agent"
    agent = Agent(name="test", workspace=ws)
    assert ws.exists()
    assert (ws / "memory").exists()


def test_agent_loads_identity(tmp_path: Path):
    ws = tmp_path / "coder"
    ws.mkdir(parents=True)
    (ws / "SOUL.md").write_text("# Soul\nI am a coding specialist.")
    (ws / "IDENTITY.md").write_text("Name: Coder\nRole: Software Engineer\nEmoji: 💻")
    (ws / "MEMORY.md").write_text("# Memory\nUser prefers Python.")
    (ws / "memory").mkdir()

    agent = Agent(name="coder", workspace=ws)
    assert agent.identity.role == "Software Engineer"
    assert agent.identity.emoji == "💻"
    assert "coding specialist" in agent.identity.soul


def test_agent_build_context(tmp_path: Path):
    ws = tmp_path / "coder"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()
    (ws / "SOUL.md").write_text("# Soul\nI help with code.")
    (ws / "MEMORY.md").write_text("# Memory\nUser likes Python.")

    agent = Agent(name="coder", workspace=ws)
    ctx = agent.build_system_context()
    assert "help with code" in ctx
    assert "likes Python" in ctx


def test_agent_ensure_defaults(tmp_path: Path):
    ws = tmp_path / "new-agent"
    agent = Agent(
        name="helper",
        workspace=ws,
        identity=AgentIdentity(name="helper", role="General assistant"),
    )
    agent.ensure_defaults()
    assert (ws / "SOUL.md").exists()
    assert (ws / "IDENTITY.md").exists()
    assert (ws / "MEMORY.md").exists()
    assert "helper" in (ws / "SOUL.md").read_text()


def test_agent_display_name():
    ident = AgentIdentity(name="Coder", emoji="💻")
    assert ident.display_name == "💻 Coder"

    ident2 = AgentIdentity(name="Helper")
    assert ident2.display_name == "Helper"


# -- Registry tests --

def test_registry_creates_default_orchestrator(registry: AgentRegistry):
    registry.load_all()
    assert len(registry) == 1
    orch = registry.get_orchestrator()
    assert orch is not None
    assert orch.name == "orchestrator"
    assert orch.is_orchestrator is True


def test_registry_loads_existing_agents(agents_dir: Path):
    # Create two agent workspaces
    coder_ws = agents_dir / "coder"
    coder_ws.mkdir(parents=True)
    (coder_ws / "SOUL.md").write_text("I code things.")
    (coder_ws / "IDENTITY.md").write_text("Name: Coder\nRole: Developer\nEmoji: 💻")
    (coder_ws / "memory").mkdir()

    researcher_ws = agents_dir / "researcher"
    researcher_ws.mkdir(parents=True)
    (researcher_ws / "SOUL.md").write_text("I research things.")
    (researcher_ws / "memory").mkdir()

    registry = AgentRegistry(agents_dir)
    registry.load_all()

    assert len(registry) == 2
    assert registry.get("coder") is not None
    assert registry.get("researcher") is not None
    assert "coder" in registry.agent_names()


def test_registry_create_agent(registry: AgentRegistry):
    agent = registry.create_agent("analyst", role="Data Analyst", emoji="📊")
    assert agent.name == "analyst"
    assert agent.identity.role == "Data Analyst"
    assert registry.get("analyst") is not None
    assert (registry.agents_dir / "analyst" / "SOUL.md").exists()


def test_registry_agent_summary(registry: AgentRegistry):
    registry.create_agent("coder", role="Developer")
    registry.create_agent("orch", role="orchestrator", is_orchestrator=True)

    summary = registry.get_agent_summary()
    assert "coder" in summary
    assert "orch" in summary
    assert "ORCHESTRATOR" in summary


# -- Orchestrator routing tests --

def test_agent_address_pattern():
    # @agent_name addressing
    m = AGENT_ADDRESS_PATTERN.match("@coder write a function")
    assert m is not None
    assert m.group(1) == "coder"
    assert m.group(2) == "write a function"

    # /agent_name addressing
    m = AGENT_ADDRESS_PATTERN.match("/researcher find info on X")
    assert m is not None
    assert m.group(1) == "researcher"

    # No addressing
    m = AGENT_ADDRESS_PATTERN.match("just a normal message")
    assert m is None


def test_orchestrator_resolve_explicit(agents_dir: Path):
    registry = AgentRegistry(agents_dir)
    registry.create_agent("coder", role="Developer")
    registry.create_agent("orchestrator", role="orchestrator", is_orchestrator=True)

    from claude_daemon.memory.store import ConversationStore
    store = ConversationStore(agents_dir.parent / "test.db")

    # We can't test auto_route without a real ProcessManager, but we can test resolve_agent
    orch = Orchestrator(registry, None, store)

    agent, msg = orch.resolve_agent("@coder write hello world")
    assert agent is not None
    assert agent.name == "coder"
    assert msg == "write hello world"

    agent, msg = orch.resolve_agent("just a question")
    assert agent is None  # No explicit addressing

    store.close()


# -- Model routing tests --

def test_agent_model_from_identity(tmp_path: Path):
    """Test that model config is read from IDENTITY.md."""
    ws = tmp_path / "opus-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()
    (ws / "IDENTITY.md").write_text(
        "Name: albert\nRole: CIO\nEmoji: 🧠\n"
        "Model: opus\nPlanning-Model: opus\nChat-Model: opus\nScheduled-Model: haiku\n"
    )

    agent = Agent(name="albert", workspace=ws)
    assert agent.get_model("default") == "opus"
    assert agent.get_model("planning") == "opus"
    assert agent.get_model("chat") == "opus"
    assert agent.get_model("scheduled") == "haiku"


def test_agent_model_defaults(tmp_path: Path):
    """Test default model values when IDENTITY.md has no model config."""
    ws = tmp_path / "basic-agent"
    ws.mkdir(parents=True)
    (ws / "memory").mkdir()

    agent = Agent(name="basic", workspace=ws)
    assert agent.get_model("default") == "sonnet"
    assert agent.get_model("planning") == "opus"
    assert agent.get_model("scheduled") == "haiku"


# -- Bootstrap tests --

def test_bootstrap_creates_csuite(tmp_path: Path):
    """Test that bootstrap creates all 7 C-suite agents."""
    agents_dir = tmp_path / "agents"
    count = create_csuite_workspaces(agents_dir)
    assert count == 7

    names = sorted(d.name for d in agents_dir.iterdir() if d.is_dir())
    assert names == ["albert", "jeremy", "johnny", "luna", "max", "penny", "sophie"]

    # Verify johnny is orchestrator
    johnny_agents = (agents_dir / "johnny" / "AGENTS.md").read_text()
    assert "orchestrator: true" in johnny_agents

    # Verify model configs
    albert_id = (agents_dir / "albert" / "IDENTITY.md").read_text()
    assert "Model: opus" in albert_id

    penny_id = (agents_dir / "penny" / "IDENTITY.md").read_text()
    assert "Model: sonnet" in penny_id


def test_bootstrap_skips_existing(tmp_path: Path):
    """Test that bootstrap doesn't overwrite existing agents."""
    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    # Modify albert's soul
    (agents_dir / "albert" / "SOUL.md").write_text("Custom soul")

    # Re-run bootstrap
    count = create_csuite_workspaces(agents_dir)
    assert count == 0  # Nothing created

    # Verify custom soul preserved
    assert (agents_dir / "albert" / "SOUL.md").read_text() == "Custom soul"


def test_shared_workspace(tmp_path: Path):
    """Test shared workspace creation."""
    create_shared_workspace(tmp_path)
    shared = tmp_path / "shared"
    assert shared.exists()
    assert (shared / "USER.md").exists()
    assert (shared / "playbooks").is_dir()
    assert (shared / "steer").is_dir()
    assert (shared / "reflections").is_dir()
    assert "Dave" in (shared / "USER.md").read_text()


def test_agent_reads_shared_user(tmp_path: Path):
    """Test that agents read shared USER.md when no local one exists."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "USER.md").write_text("Name: Dave\nRole: Chairman")

    ws = tmp_path / "agent"
    agent = Agent(name="test", workspace=ws, shared_dir=shared)
    assert "Dave" in agent.identity.user_context
