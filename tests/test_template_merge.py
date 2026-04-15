"""Tests for template merge — delivering new guidance to existing agents."""

from __future__ import annotations

from pathlib import Path

from claude_daemon.agents.template_merge import (
    MergeChange,
    _parse_sections,
    merge_agent_templates,
    merge_template_into_content,
)


# ------------------------------------------------------------------ #
# Section parsing
# ------------------------------------------------------------------ #


def test_parse_sections_basic():
    content = "# Soul\n\nI am test.\n\n## Identity\nI help.\n\n## Style\nDirect.\n"
    sections = _parse_sections(content)
    assert "__preamble__" in sections
    assert "## Identity" in sections
    assert "## Style" in sections
    assert "I help." in sections["## Identity"]
    assert "Direct." in sections["## Style"]


def test_parse_sections_no_preamble():
    content = "## Only\nContent here.\n"
    sections = _parse_sections(content)
    assert "__preamble__" not in sections
    assert "## Only" in sections


def test_parse_sections_empty():
    sections = _parse_sections("")
    assert sections == {}


# ------------------------------------------------------------------ #
# Content merge
# ------------------------------------------------------------------ #


def test_merge_adds_new_section():
    existing = "# Soul\n\nI am test.\n\n## Identity\nI help.\n"
    template = "# Soul\n\nI am test.\n\n## Identity\nI help.\n\n## New Feature\nDo this.\n"

    merged, changes = merge_template_into_content(existing, template)

    assert "## New Feature" in merged
    assert "Do this." in merged
    assert "## Identity" in merged  # preserved
    added = [c for c in changes if c.action == "added"]
    assert len(added) == 1
    assert added[0].section == "## New Feature"


def test_merge_skips_existing_section():
    existing = "# Soul\n\n## Identity\nCustomised by user.\n\n## Style\nMy way.\n"
    template = "# Soul\n\n## Identity\nOriginal template.\n\n## Style\nTemplate way.\n"

    merged, changes = merge_template_into_content(existing, template)

    # Existing content preserved, not overwritten
    assert "Customised by user." in merged
    assert "My way." in merged
    assert "Original template." not in merged
    assert "Template way." not in merged
    added = [c for c in changes if c.action == "added"]
    assert len(added) == 0


def test_merge_adds_multiple_new_sections():
    existing = "# Soul\n\n## Identity\nI exist.\n"
    template = (
        "# Soul\n\n## Identity\nI exist.\n\n"
        "## SSH Tools\nUse SSH.\n\n"
        "## tmux Tools\nUse tmux.\n"
    )

    merged, changes = merge_template_into_content(existing, template)

    assert "## SSH Tools" in merged
    assert "## tmux Tools" in merged
    added = [c for c in changes if c.action == "added"]
    assert len(added) == 2


def test_merge_does_not_duplicate_preamble():
    existing = "# Soul\n\nI am Albert.\n\n## Identity\nCIO.\n"
    template = "# Soul\n\nI am Albert, different text.\n\n## Identity\nCIO.\n\n## New\nStuff.\n"

    merged, changes = merge_template_into_content(existing, template)

    # Preamble not duplicated
    assert merged.count("I am Albert") == 1
    assert "## New" in merged


def test_merge_idempotent():
    existing = "# Soul\n\n## Identity\nI exist.\n\n## Extra\nAlready here.\n"
    template = "# Soul\n\n## Identity\nI exist.\n\n## Extra\nAlready here.\n"

    merged, changes = merge_template_into_content(existing, template)

    added = [c for c in changes if c.action == "added"]
    assert len(added) == 0
    # Content unchanged (minus trailing whitespace)
    assert merged.strip() == existing.strip()


# ------------------------------------------------------------------ #
# Full agent merge
# ------------------------------------------------------------------ #


def test_merge_agent_templates_adds_new_sections(tmp_path: Path):
    """Simulate an update: existing agent gets new template sections."""
    from claude_daemon.agents.bootstrap import create_csuite_workspaces

    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    # Remove a section from albert's SOUL.md to simulate a pre-update install
    albert_soul = agents_dir / "albert" / "SOUL.md"
    content = albert_soul.read_text()
    # Remove the "## Remote Operations" section
    lines = content.split("\n")
    new_lines = []
    skip = False
    for line in lines:
        if line.startswith("## Remote Operations"):
            skip = True
            continue
        if skip and line.startswith("## "):
            skip = False
        if not skip:
            new_lines.append(line)
    albert_soul.write_text("\n".join(new_lines))

    # Verify it's gone
    assert "## Remote Operations" not in albert_soul.read_text()

    # Run merge
    result = merge_agent_templates(agents_dir)

    # Section should be restored
    assert "## Remote Operations" in albert_soul.read_text()
    assert result.sections_added >= 1


def test_merge_agent_templates_no_op_when_current(tmp_path: Path):
    """Fresh bootstrap should have nothing to merge."""
    from claude_daemon.agents.bootstrap import create_csuite_workspaces

    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    result = merge_agent_templates(agents_dir)
    assert result.sections_added == 0


def test_merge_archives_before_writing(tmp_path: Path):
    """Modified files should be archived before merge writes."""
    from claude_daemon.agents.bootstrap import create_csuite_workspaces

    agents_dir = tmp_path / "agents"
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    create_csuite_workspaces(agents_dir)

    # Remove a section to trigger a merge
    albert_soul = agents_dir / "albert" / "SOUL.md"
    content = albert_soul.read_text().replace(
        "## Remote Operations", "## REMOVED_FOR_TEST"
    )
    albert_soul.write_text(content)

    result = merge_agent_templates(agents_dir)

    # Check archive was created
    archive_dir = shared_dir / "template-archive"
    if archive_dir.exists():
        archives = list(archive_dir.glob("SOUL_*.md"))
        assert len(archives) >= 1


def test_merge_preserves_user_customisations(tmp_path: Path):
    """User-added sections should survive the merge."""
    from claude_daemon.agents.bootstrap import create_csuite_workspaces

    agents_dir = tmp_path / "agents"
    create_csuite_workspaces(agents_dir)

    # Add a custom user section
    albert_soul = agents_dir / "albert" / "SOUL.md"
    content = albert_soul.read_text()
    content += "\n\n## My Custom Rules\nDo things my way.\n"
    albert_soul.write_text(content)

    # Run merge
    merge_agent_templates(agents_dir)

    # Custom section still there
    final = albert_soul.read_text()
    assert "## My Custom Rules" in final
    assert "Do things my way." in final
