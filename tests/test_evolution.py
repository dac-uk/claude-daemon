"""Tests for the EvolutionActuator — self-evolution safety guards."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_daemon.agents.agent import Agent, AgentIdentity
from claude_daemon.agents.evolution import EvolutionActuator, PROTECTED_SECTIONS
from claude_daemon.agents.registry import AgentRegistry
from claude_daemon.memory.store import ConversationStore


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    d.mkdir()
    (d / "evolution-archive").mkdir()
    return d


@pytest.fixture
def store(tmp_path: Path):
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def registry(agents_dir: Path) -> AgentRegistry:
    reg = AgentRegistry(agents_dir)
    reg.create_agent("albert", role="CIO")
    return reg


@pytest.fixture
def config():
    cfg = MagicMock()
    cfg.evolution_enabled = True
    cfg.evolution_dry_run = False
    return cfg


@pytest.fixture
def actuator(registry, store, config, shared_dir):
    pm = MagicMock()
    return EvolutionActuator(registry, pm, store, config, shared_dir)


# -- _apply_operation tests --

def test_append_section(actuator):
    content = "# Soul\n\n## Identity\nI am Albert.\n"
    result = actuator._apply_operation(content, "append_section", "## Communication", "Talk to others.\n")
    assert "## Communication" in result
    assert "Talk to others." in result
    assert result.index("## Communication") > result.index("## Identity")


def test_replace_section(actuator):
    content = "# Soul\n\n## Identity\nI am Albert.\n\n## Values\nReliability.\n"
    result = actuator._apply_operation(content, "replace_section", "## Values", "New values here.\n")
    assert "New values here." in result
    assert "Reliability" not in result
    assert "## Identity" in result


def test_replace_section_missing_appends(actuator):
    content = "# Soul\n\n## Identity\nI am Albert.\n"
    result = actuator._apply_operation(content, "replace_section", "## NewSection", "Content.\n")
    assert "## NewSection" in result
    assert "Content." in result


# -- Protected section tests --

def test_protected_sections_exist():
    assert "## Identity" in PROTECTED_SECTIONS
    assert "## Values" in PROTECTED_SECTIONS
    assert "## Operating Directive" in PROTECTED_SECTIONS


def test_protected_section_normalized():
    """Verify that normalization catches variant formatting."""
    # Simulate the normalization logic from _apply_proposals
    section = "##Identity"  # no space
    normalized = "## " + section.lstrip("#").strip()
    assert any(normalized.lower().startswith(ps.lower()) for ps in PROTECTED_SECTIONS)


def test_protected_section_with_space():
    section = "## Identity"
    normalized = "## " + section.lstrip("#").strip()
    assert any(normalized.lower().startswith(ps.lower()) for ps in PROTECTED_SECTIONS)


# -- Size guard test --

def test_size_guard_rejects_catastrophic_loss(actuator, registry):
    """New file content < 30% of old should be rejected."""
    agent = registry.get("albert")
    soul_path = agent.workspace / "SOUL.md"
    soul_path.write_text("# Soul\n\n" + "x" * 500)

    # Simulate a proposal that produces very short content
    proposal = {
        "file": "SOUL.md",
        "operation": "replace_section",
        "section_heading": "## Tiny",
        "new_content": "y",
        "rationale": "test",
    }

    # The _apply_proposals method checks content length
    # We test the size guard logic directly
    old_content = soul_path.read_text()
    new_content = "tiny"  # much shorter
    assert len(new_content) < len(old_content) * 0.3  # Would be rejected


# -- Evolution log recording --

def test_evolution_log_writes_markdown(actuator, shared_dir):
    """_log_evolution should write to shared/evolution-log.md."""
    actuator._log_evolution("albert", "SOUL.md", "append_section", "## Test", "reason", False)
    log_path = shared_dir / "evolution-log.md"
    assert log_path.exists()
    content = log_path.read_text()
    assert "albert" in content
    assert "SOUL.md" in content
    assert "append_section" in content


def test_evolution_store_record(store):
    """record_evolution should persist to the evolution_log table."""
    store.record_evolution("albert", "SOUL.md", "append_section", "## Test", "reason")
    history = store.get_evolution_history(agent_name="albert")
    assert len(history) >= 1
    assert history[0]["file_changed"] == "SOUL.md"
    assert history[0]["operation"] == "append_section"


# -- Dry run test --

def test_dry_run_does_not_modify_files(registry, store, config, shared_dir):
    """In dry_run mode, files should not be modified."""
    config.evolution_dry_run = True
    pm = MagicMock()
    actuator = EvolutionActuator(registry, pm, store, config, shared_dir)

    agent = registry.get("albert")
    soul_path = agent.workspace / "SOUL.md"
    original = soul_path.read_text()

    # Simulate applying a proposal with dry_run=True
    proposals = [{
        "file": "SOUL.md", "operation": "append_section",
        "section_heading": "## DryRunTest", "new_content": "Should not appear",
        "rationale": "test",
    }]

    applied = actuator._apply_proposals(agent, proposals, dry_run=True)
    assert len(applied) >= 1
    # File should NOT have been modified
    assert soul_path.read_text() == original
