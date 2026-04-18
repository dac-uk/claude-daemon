"""Tests for the shared brain builder and CLAUDE.md installer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_daemon.agents.registry import AgentRegistry
from claude_daemon.agents.shared_brain import (
    BEGIN_SENTINEL,
    END_SENTINEL,
    SharedBrainBuilder,
    brain_status,
    condense_soul,
    install_into_claude_md,
    latest_reflections,
    list_skills,
    top_memory_entries,
    uninstall_from_claude_md,
)


# -- Fixtures -----------------------------------------------------------------


def _make_agent_workspace(
    agents_dir: Path,
    name: str,
    role: str,
    emoji: str,
    soul: str,
    memory: str = "",
    reflections: str = "",
    tools: dict | None = None,
) -> Path:
    ws = agents_dir / name
    ws.mkdir(parents=True)
    (ws / "SOUL.md").write_text(soul, encoding="utf-8")
    (ws / "IDENTITY.md").write_text(
        f"# Identity\n\nName: {name}\nRole: {role}\nEmoji: {emoji}\nModel: sonnet\n",
        encoding="utf-8",
    )
    (ws / "MEMORY.md").write_text(memory, encoding="utf-8")
    if reflections:
        (ws / "REFLECTIONS.md").write_text(reflections, encoding="utf-8")
    if tools is not None:
        (ws / "tools.json").write_text(json.dumps(tools), encoding="utf-8")
    return ws


@pytest.fixture
def daemon_tree(tmp_path):
    """Fabricate a minimal daemon data dir with 3 agents and shared/ files."""
    data_dir = tmp_path / "data"
    agents_dir = data_dir / "agents"
    shared_dir = data_dir / "shared"
    agents_dir.mkdir(parents=True)
    shared_dir.mkdir(parents=True)

    _make_agent_workspace(
        agents_dir, "orchestrator", "orchestrator", "🎛️",
        "# Soul\n\nI coordinate the team.\n\n## Values\n- Clarity\n- Follow-through\n",
        memory="# Memory\n\n- Met the user 2026-04-01\n- Fixed SDK timeout\n",
        tools={"mcpServers": {"filesystem": {}, "git": {}}},
    )
    _make_agent_workspace(
        agents_dir, "coder", "engineering lead", "🛠️",
        "# Soul\n\nI build and ship code.\n",
        memory="# Memory\n\n- Shipped phase 13 on 2026-04-10\n",
        reflections="# Reflections\n\n## 2026-04-15\nLearned to always run pytest before commit.\n",
        tools={"mcpServers": {"github": {}, "filesystem": {}}},
    )
    _make_agent_workspace(
        agents_dir, "researcher", "researcher", "🔎",
        "# Soul\n\nI dig into docs and papers.\n",
        memory="",
        tools={"mcpServers": {"tavily": {}}},
    )

    (shared_dir / "DIRECTIVE.md").write_text(
        "# Directive\n\nAlways verify before reporting done.\n", encoding="utf-8",
    )
    (shared_dir / "USER.md").write_text(
        "User: David. Timezone: Europe/London.\n", encoding="utf-8",
    )
    (shared_dir / "events.md").write_text(
        "\n".join(f"2026-04-{d:02d} event line {d}" for d in range(1, 16)) + "\n",
        encoding="utf-8",
    )
    (shared_dir / "learnings.md").write_text(
        "- Prefer small PRs\n- Test before merging\n", encoding="utf-8",
    )
    (shared_dir / "playbooks").mkdir()
    (shared_dir / "playbooks" / "improvement-plan.md").write_text(
        "# Plan\n\n1. Reduce heartbeat cost by 30%\n"
        "2. Add reactive shared-brain regen\n"
        "3. Ship memory compaction weekly\n"
        "4. Polish the dashboard\n",
        encoding="utf-8",
    )

    return data_dir


@pytest.fixture
def registry(daemon_tree):
    reg = AgentRegistry(
        agents_dir=daemon_tree / "agents",
        shared_dir=daemon_tree / "shared",
    )
    reg.load_all()
    return reg


# -- Condensation helpers -----------------------------------------------------


def test_condense_soul_drops_h1_and_respects_word_budget():
    soul = "# Soul\n\nFirst paragraph. " + "word " * 300
    out = condense_soul(soul, target_words=50)
    assert not out.startswith("# ")
    assert len(out.split()) <= 50 + 1  # allow ellipsis


def test_condense_soul_short_input_returned_verbatim():
    soul = "# Soul\n\nShort and sweet."
    out = condense_soul(soul, target_words=150)
    assert "Short and sweet." in out


def test_top_memory_entries_most_recent_first():
    memory = "# Memory\n\n" + "\n".join(f"- entry {i}" for i in range(1, 21))
    entries = top_memory_entries(memory, n=5)
    assert entries == [f"entry {i}" for i in range(20, 15, -1)]


def test_latest_reflections_returns_last_n():
    refl = "# Reflections\n\n## 2026-01\nA\n\n## 2026-02\nB\n\n## 2026-03\nC\n"
    out = latest_reflections(refl, n=2)
    assert len(out) == 2
    assert out[0].startswith("2026-03")


def test_list_skills_from_tools_json(tmp_path):
    tools = tmp_path / "tools.json"
    tools.write_text(
        json.dumps({"mcpServers": {"github": {}, "slack": {}, "gmail": {}}}),
        encoding="utf-8",
    )
    assert list_skills(tools) == ["github", "gmail", "slack"]


def test_list_skills_malformed_json_returns_empty(tmp_path):
    tools = tmp_path / "tools.json"
    tools.write_text("{not valid", encoding="utf-8")
    assert list_skills(tools) == []


def test_list_skills_missing_file_returns_empty(tmp_path):
    assert list_skills(tmp_path / "missing.json") == []


# -- Builder ------------------------------------------------------------------


def test_builder_contains_all_agents(registry, daemon_tree, tmp_path):
    out = tmp_path / "brain.md"
    content = SharedBrainBuilder(
        registry=registry,
        shared_dir=daemon_tree / "shared",
        output_path=out,
    ).build()
    # Roster row + per-agent section header for each agent
    for name in ("orchestrator", "coder", "researcher"):
        assert name in content
    assert "### 🎛️ orchestrator" in content
    assert "### 🛠️ coder" in content
    assert "Skills (MCP servers):" in content


def test_builder_includes_shared_context(registry, daemon_tree, tmp_path):
    content = SharedBrainBuilder(
        registry=registry,
        shared_dir=daemon_tree / "shared",
        output_path=tmp_path / "brain.md",
    ).build()
    assert "### Directive" in content
    assert "verify before reporting done" in content
    assert "### User" in content
    assert "David" in content
    assert "## Active Improvement Plan" in content
    assert "Reduce heartbeat cost" in content


def test_builder_recent_events_last_10_lines(registry, daemon_tree, tmp_path):
    content = SharedBrainBuilder(
        registry=registry,
        shared_dir=daemon_tree / "shared",
        output_path=tmp_path / "brain.md",
    ).build()
    # There are 15 event lines; only the last 10 should appear.
    assert "event line 15" in content
    assert "event line 6" in content
    assert "event line 5" not in content


def test_size_bound_enforced(daemon_tree, tmp_path):
    # Rewrite SOULs with huge content
    for name in ("orchestrator", "coder", "researcher"):
        (daemon_tree / "agents" / name / "SOUL.md").write_text(
            "# Soul\n\n" + "lorem ipsum " * 3000,
            encoding="utf-8",
        )
    reg = AgentRegistry(
        agents_dir=daemon_tree / "agents",
        shared_dir=daemon_tree / "shared",
    )
    reg.load_all()
    content = SharedBrainBuilder(
        registry=reg,
        shared_dir=daemon_tree / "shared",
        output_path=tmp_path / "brain.md",
        max_chars=5_000,
    ).build()
    assert len(content) <= 5_000


def test_shared_sections_never_truncated_under_stress(daemon_tree, tmp_path):
    for name in ("orchestrator", "coder", "researcher"):
        (daemon_tree / "agents" / name / "SOUL.md").write_text(
            "# Soul\n\n" + "lorem ipsum " * 3000,
            encoding="utf-8",
        )
    reg = AgentRegistry(
        agents_dir=daemon_tree / "agents",
        shared_dir=daemon_tree / "shared",
    )
    reg.load_all()
    content = SharedBrainBuilder(
        registry=reg,
        shared_dir=daemon_tree / "shared",
        output_path=tmp_path / "brain.md",
        max_chars=4_000,
    ).build()
    # Shared context headings must still appear even under stress
    assert "## Shared Context" in content
    assert "### Directive" in content
    assert "## Active Improvement Plan" in content


def test_no_agents_registered_placeholder(tmp_path):
    data_dir = tmp_path / "data"
    agents_dir = data_dir / "agents"
    shared_dir = data_dir / "shared"
    agents_dir.mkdir(parents=True)
    shared_dir.mkdir(parents=True)
    (shared_dir / "DIRECTIVE.md").write_text("be good", encoding="utf-8")

    # Use an empty registry without triggering default orchestrator creation
    reg = AgentRegistry(agents_dir=agents_dir, shared_dir=shared_dir)
    # Intentionally skip load_all() so registry stays empty
    content = SharedBrainBuilder(
        registry=reg,
        shared_dir=shared_dir,
        output_path=tmp_path / "brain.md",
    ).build()
    assert "No agents registered yet" in content
    assert "### Directive" in content  # shared context still present


def test_write_produces_file(registry, daemon_tree, tmp_path):
    out = tmp_path / "brain.md"
    builder = SharedBrainBuilder(
        registry=registry,
        shared_dir=daemon_tree / "shared",
        output_path=out,
    )
    path = builder.write()
    assert path == out
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("# Daemon Shared Brain")


def test_atomic_write_on_replace_failure(registry, daemon_tree, tmp_path, monkeypatch):
    out = tmp_path / "brain.md"
    out.write_text("ORIGINAL", encoding="utf-8")

    def fake_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", fake_replace)

    builder = SharedBrainBuilder(
        registry=registry,
        shared_dir=daemon_tree / "shared",
        output_path=out,
    )
    with pytest.raises(OSError):
        builder.write()
    # Original file must be untouched
    assert out.read_text(encoding="utf-8") == "ORIGINAL"


# -- CLAUDE.md install/uninstall ----------------------------------------------


def test_install_adds_sentinel_block(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    brain = tmp_path / "brain.md"
    brain.write_text("# Brain", encoding="utf-8")

    changed = install_into_claude_md(brain, claude_md)
    assert changed is True
    content = claude_md.read_text(encoding="utf-8")
    assert BEGIN_SENTINEL in content
    assert END_SENTINEL in content
    # @path line present
    assert "@" in content
    assert "brain.md" in content


def test_install_idempotent(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    brain = tmp_path / "brain.md"
    brain.write_text("x", encoding="utf-8")

    install_into_claude_md(brain, claude_md)
    install_into_claude_md(brain, claude_md)

    content = claude_md.read_text(encoding="utf-8")
    assert content.count(BEGIN_SENTINEL) == 1
    assert content.count(END_SENTINEL) == 1


def test_install_preserves_surrounding_content(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    original = "# My own instructions\n\nDo X, not Y.\n"
    claude_md.write_text(original, encoding="utf-8")
    brain = tmp_path / "brain.md"
    brain.write_text("x", encoding="utf-8")

    install_into_claude_md(brain, claude_md)
    uninstall_from_claude_md(claude_md)

    # Round-trip should preserve user content (allow trailing newline diff)
    final = claude_md.read_text(encoding="utf-8")
    assert final.rstrip() == original.rstrip()


def test_uninstall_exact_removal(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    unrelated = "Prelude.\n\n# Other heading\n\nBody.\n"
    claude_md.write_text(unrelated, encoding="utf-8")
    brain = tmp_path / "brain.md"
    brain.write_text("x", encoding="utf-8")

    install_into_claude_md(brain, claude_md)
    assert BEGIN_SENTINEL in claude_md.read_text(encoding="utf-8")

    uninstall_from_claude_md(claude_md)
    final = claude_md.read_text(encoding="utf-8")
    assert BEGIN_SENTINEL not in final
    assert END_SENTINEL not in final
    # Unrelated content still present
    assert "Prelude." in final
    assert "Other heading" in final
    assert "Body." in final


def test_uninstall_missing_file_is_noop(tmp_path):
    claude_md = tmp_path / "no-such-file.md"
    assert uninstall_from_claude_md(claude_md) is False


def test_brain_status_reports_install_state(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    brain = tmp_path / "brain.md"
    brain.write_text("hello", encoding="utf-8")

    info = brain_status(brain, claude_md)
    assert info["brain_exists"] is True
    assert info["installed"] is False
    assert info["size_bytes"] == len("hello")

    install_into_claude_md(brain, claude_md)
    info2 = brain_status(brain, claude_md)
    assert info2["installed"] is True
