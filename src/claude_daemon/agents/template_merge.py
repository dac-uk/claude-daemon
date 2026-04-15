"""Template merge — deliver new guidance to existing agent files without overwriting.

When the daemon code is updated, new SOUL.md / AGENTS.md template content
(e.g. new ## sections) should reach existing installations. But we must never
overwrite sections that users or the EvolutionActuator have customised.

Strategy: parse both the on-disk file and the code template into ## sections.
Append any sections that exist in the template but are missing from the file.
Never touch sections that already exist on disk (even if the template version
is newer). Archive before writing.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Matches `## Heading` lines and captures the heading text
_SECTION_RE = re.compile(r"^(## .+)$", re.MULTILINE)


@dataclass
class MergeChange:
    """Record of a single section merge action."""

    file: str
    section: str
    action: str  # "added" | "skipped" | "archived"


@dataclass
class MergeResult:
    """Aggregate result of a template merge run."""

    changes: list[MergeChange] = field(default_factory=list)

    @property
    def sections_added(self) -> int:
        return sum(1 for c in self.changes if c.action == "added")

    def summary(self) -> str:
        if not self.changes:
            return "No template changes needed."
        added = [c for c in self.changes if c.action == "added"]
        if not added:
            return "Templates up to date."
        lines = [f"Template merge: {len(added)} new section(s) added:"]
        for c in added:
            lines.append(f"  - {c.file}: {c.section}")
        return "\n".join(lines)


def _parse_sections(content: str) -> dict[str, str]:
    """Parse markdown into {heading: full_block} pairs.

    Returns a dict where keys are '## Heading' strings and values are
    the complete block text (heading + body up to next ## or EOF).
    The preamble (text before the first ##) is stored under key '__preamble__'.
    """
    sections: dict[str, str] = {}
    parts = _SECTION_RE.split(content)

    # parts alternates: [preamble, heading1, body1, heading2, body2, ...]
    if parts:
        preamble = parts[0].strip()
        if preamble:
            sections["__preamble__"] = preamble

    i = 1
    while i < len(parts) - 1:
        heading = parts[i].strip()
        body = parts[i + 1].rstrip()
        sections[heading] = f"{heading}\n{body}"
        i += 2

    return sections


def merge_template_into_content(
    existing: str,
    template: str,
) -> tuple[str, list[MergeChange]]:
    """Merge new sections from template into existing content.

    Only appends sections whose ## heading doesn't already exist.
    Never modifies or removes existing sections.

    Returns (merged_content, changes).
    """
    existing_sections = _parse_sections(existing)
    template_sections = _parse_sections(template)
    changes: list[MergeChange] = []
    merged = existing.rstrip()

    for heading, block in template_sections.items():
        if heading == "__preamble__":
            continue  # Don't merge preamble (agent identity text)
        if heading in existing_sections:
            changes.append(MergeChange(file="", section=heading, action="skipped"))
            continue
        # New section — append
        merged = merged + "\n\n" + block
        changes.append(MergeChange(file="", section=heading, action="added"))

    return merged + "\n", changes


def _archive_before_write(path: Path, archive_dir: Path) -> Path | None:
    """Create a timestamped backup before modifying a file."""
    if not path.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = archive_dir / f"{path.stem}_{ts}.md"
    shutil.copy2(path, dest)
    return dest


def merge_agent_templates(agents_dir: Path) -> MergeResult:
    """Merge new template sections into all existing agent files.

    For each agent in CSUITE_AGENTS, compares the on-disk SOUL.md (and
    AGENTS.md, GOTCHAS.md) against the code template. Appends any new
    ## sections that don't already exist. Archives before writing.

    Safe to run on every startup — idempotent (no-op when up to date).
    """
    from claude_daemon.agents.bootstrap import CSUITE_AGENTS

    result = MergeResult()

    if not agents_dir.is_dir():
        return result

    # Map of file_key -> template_key in agent_def
    file_templates = {
        "SOUL.md": "soul",
        "AGENTS.md": "agents_rules",
    }

    archive_dir = agents_dir.parent / "shared" / "template-archive"

    for agent_def in CSUITE_AGENTS:
        name = agent_def["name"]
        workspace = agents_dir / name

        if not workspace.is_dir():
            continue

        for filename, template_key in file_templates.items():
            template_content = agent_def.get(template_key, "")
            if not template_content:
                continue

            file_path = workspace / filename
            if not file_path.exists():
                # File doesn't exist yet — write the full template
                file_path.write_text(template_content)
                result.changes.append(
                    MergeChange(file=f"{name}/{filename}", section="(full file)", action="added")
                )
                continue

            existing = file_path.read_text()
            merged, changes = merge_template_into_content(existing, template_content)

            # Tag changes with the file path
            for c in changes:
                c.file = f"{name}/{filename}"

            new_sections = [c for c in changes if c.action == "added"]
            if not new_sections:
                continue  # Nothing to add

            # Archive before writing
            archived = _archive_before_write(file_path, archive_dir)
            if archived:
                result.changes.append(
                    MergeChange(
                        file=f"{name}/{filename}", section="(archive)", action="archived"
                    )
                )
                log.info("Archived %s/%s before merge", name, filename)

            file_path.write_text(merged)
            result.changes.extend(new_sections)
            log.info(
                "Merged %d new section(s) into %s/%s: %s",
                len(new_sections),
                name,
                filename,
                ", ".join(c.section for c in new_sections),
            )

    if result.sections_added:
        log.info("Template merge complete: %d sections added", result.sections_added)
    return result
