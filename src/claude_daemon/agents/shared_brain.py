"""SharedBrainBuilder — render the daemon's collective state as a single
markdown digest that Claude Code CLI and the macOS app can @-import.

The output file lives at `~/.config/claude-daemon/shared-brain.md` by
default. Installing via `install_into_claude_md()` appends a
sentinel-wrapped `@path` line to `~/.claude/CLAUDE.md` so both Claude
Code CLI and the macOS app pick it up automatically.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_daemon.agents.agent import Agent
from claude_daemon.agents.registry import AgentRegistry

log = logging.getLogger(__name__)


DEFAULT_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"

BEGIN_SENTINEL = "<!-- BEGIN claude-daemon shared-brain (managed, do not edit) -->"
END_SENTINEL = "<!-- END claude-daemon shared-brain -->"
_SENTINEL_RE = re.compile(
    re.escape(BEGIN_SENTINEL) + r".*?" + re.escape(END_SENTINEL),
    re.DOTALL,
)


# -- Condensation helpers -----------------------------------------------------


def condense_soul(soul: str, target_words: int = 150) -> str:
    """Compact a SOUL.md to roughly target_words words.

    Strips top-level markdown headings and keeps the first paragraph
    plus any bullet list that follows, clipped to the word budget.
    """
    if not soul:
        return ""
    # Drop top-level # heading ("# Soul"); keep ## subsections as-is
    lines = [ln for ln in soul.splitlines() if not ln.lstrip().startswith("# ")]
    text = "\n".join(lines).strip()
    words = text.split()
    if len(words) <= target_words:
        return text
    return " ".join(words[:target_words]).rstrip(",;:") + "…"


def top_memory_entries(memory_md: str, n: int = 5) -> list[str]:
    """Return up to n most-recent bullet entries from a MEMORY.md.

    MEMORY.md is freeform but agents tend to append dated bullets at
    the bottom. We read from the bottom up, collecting any line that
    starts with `-` or `*` (possibly indented).
    """
    if not memory_md:
        return []
    entries: list[str] = []
    for line in reversed(memory_md.splitlines()):
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            entries.append(stripped[2:].strip())
            if len(entries) >= n:
                break
    return entries


def latest_reflections(reflections_md: str, n: int = 3) -> list[str]:
    """Return up to n most-recent reflections.

    REFLECTIONS.md uses `## <date or title>` sections. Take the last n.
    """
    if not reflections_md:
        return []
    sections = re.split(r"^##\s+", reflections_md, flags=re.MULTILINE)
    # First split piece is pre-heading content (usually the H1 header).
    body_sections = [s.strip() for s in sections[1:] if s.strip()]
    return body_sections[-n:][::-1]  # most-recent first


def list_skills(tools_json_path: Path | None) -> list[str]:
    """Return MCP server names from an agent's tools.json, or []."""
    if not tools_json_path or not tools_json_path.exists():
        return []
    try:
        data = json.loads(tools_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    return sorted(servers.keys())


# -- Builder ------------------------------------------------------------------


@dataclass
class SharedBrainBuilder:
    """Builds and writes the shared brain markdown file."""

    registry: AgentRegistry
    shared_dir: Path
    output_path: Path
    max_chars: int = 30_000

    def build(self) -> str:
        """Return the shared brain markdown (not written to disk)."""
        fixed = self._render_fixed_sections()
        agents = list(self.registry)

        if not agents:
            return fixed  # already contains "No agents registered yet." roster

        # Tiered per-agent rendering: try progressively smaller budgets.
        levels = [
            (150, 5, 3),
            (100, 3, 2),
            (60, 2, 1),
            (30, 1, 0),
            (0, 0, 0),  # name + role only
        ]
        for soul_words, mem_n, refl_n in levels:
            agent_blocks = [
                self._render_agent(a, soul_words, mem_n, refl_n) for a in agents
            ]
            agents_section = "## Agents\n\n" + "\n\n---\n\n".join(agent_blocks)
            total = fixed.replace("<!--AGENTS-->", agents_section)
            if len(total) <= self.max_chars:
                return total

        # Last resort — fixed alone or truncated agents.
        fallback = fixed.replace(
            "<!--AGENTS-->",
            "## Agents\n\n_(agent details dropped to fit size budget)_",
        )
        if len(fallback) > self.max_chars:
            log.warning(
                "shared brain fixed sections (%d bytes) exceed max_chars (%d); "
                "emitting verbatim",
                len(fallback),
                self.max_chars,
            )
        return fallback

    def write(self) -> Path:
        """Build and atomically write the shared brain to output_path."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        content = self.build()

        lock_path = self.output_path.with_suffix(self.output_path.suffix + ".lock")
        lock_fd: int | None = None
        try:
            lock_fd = _try_lock(lock_path)
            if lock_fd is None:
                log.debug("shared brain regen skipped — another writer holds the lock")
                return self.output_path

            # Atomic write: tmpfile in same dir, then os.replace
            fd, tmp_name = tempfile.mkstemp(
                prefix=".shared-brain.", suffix=".tmp",
                dir=str(self.output_path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_name, self.output_path)
            except Exception:
                # Best-effort cleanup if rename never happened.
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                raise
        finally:
            if lock_fd is not None:
                _release_lock(lock_fd, lock_path)

        return self.output_path

    def regenerate(self) -> Path:
        return self.write()

    # -- Internal rendering --

    def _render_fixed_sections(self) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        agents = list(self.registry)

        header = (
            "# Daemon Shared Brain\n"
            f"_Generated: {now} — refresh with `claude-daemon shared-brain sync`_\n\n"
            "This file is maintained by claude-daemon. It gives Claude Code\n"
            "CLI and the macOS app awareness of the daemon's agents, their\n"
            "identities, curated memories, and available skills.\n"
        )

        # Roster
        roster_lines = ["## Roster\n"]
        if agents:
            roster_lines.append("| Emoji | Agent | Role | Purpose |")
            roster_lines.append("|-------|-------|------|---------|")
            for agent in agents:
                emoji = agent.identity.emoji or ""
                role = agent.identity.role or "—"
                purpose = _first_meaningful_line(agent.identity.soul) or "—"
                purpose = purpose.replace("|", "/")[:80]
                roster_lines.append(
                    f"| {emoji} | {agent.name} | {role} | {purpose} |"
                )
        else:
            roster_lines.append("_No agents registered yet._")

        roster = "\n".join(roster_lines) + "\n"

        # Per-agent section placeholder (filled in by build())
        agents_placeholder = "<!--AGENTS-->"

        # Shared context
        shared_blocks: list[str] = ["## Shared Context\n"]
        directive = _read_or_empty(self.shared_dir / "DIRECTIVE.md")
        if directive:
            shared_blocks.append(f"### Directive\n\n{directive}\n")
        user = _read_or_empty(self.shared_dir / "USER.md")
        if user:
            shared_blocks.append(f"### User\n\n{user}\n")
        events = _read_or_empty(self.shared_dir / "events.md")
        if events:
            tail = "\n".join(events.splitlines()[-10:])
            shared_blocks.append(f"### Recent events\n\n{tail}\n")
        learnings = _read_or_empty(self.shared_dir / "learnings.md")
        if learnings:
            excerpt = _word_clip(learnings, 200)
            shared_blocks.append(f"### Team learnings\n\n{excerpt}\n")
        shared = "\n".join(shared_blocks)

        # Improvement plan
        plan_path = self.shared_dir / "playbooks" / "improvement-plan.md"
        plan_md = _read_or_empty(plan_path)
        if plan_md:
            plan_section = (
                "## Active Improvement Plan\n\n"
                + _top_list_items(plan_md, 3)
                + "\n"
            )
        else:
            plan_section = ""

        footer = (
            "## Delegating to a specific agent\n\n"
            "From Claude Code CLI or the macOS app, mention an agent by "
            "name (e.g. `ask the orchestrator to …`) to route work through "
            "the daemon. Each agent has its own skills (MCP servers) listed "
            "above.\n"
        )

        return "\n".join([header, roster, agents_placeholder, shared, plan_section, footer]).strip() + "\n"

    def _render_agent(
        self,
        agent: Agent,
        soul_words: int,
        mem_n: int,
        refl_n: int,
    ) -> str:
        ident = agent.identity
        emoji = f"{ident.emoji} " if ident.emoji else ""
        header = f"### {emoji}{agent.name}"

        parts = [header]
        models = (
            f"**Role:** {ident.role or '—'}  |  "
            f"**Models:** default={ident.default_model} "
            f"planning={ident.planning_model} "
            f"chat={ident.chat_model} "
            f"scheduled={ident.scheduled_model}"
        )
        parts.append(models)

        if soul_words > 0 and ident.soul:
            condensed = condense_soul(ident.soul, soul_words)
            if condensed:
                parts.append(f"**Identity (condensed):**\n\n{condensed}")

        if mem_n > 0:
            mem_path = agent.workspace / "MEMORY.md"
            if mem_path.exists():
                entries = top_memory_entries(mem_path.read_text(encoding="utf-8"), mem_n)
                if entries:
                    bullets = "\n".join(f"- {e}" for e in entries)
                    parts.append(f"**Recent memory:**\n\n{bullets}")

        if refl_n > 0:
            refl_path = agent.workspace / "REFLECTIONS.md"
            if refl_path.exists():
                refls = latest_reflections(refl_path.read_text(encoding="utf-8"), refl_n)
                if refls:
                    bullets = "\n".join(
                        f"- {_first_meaningful_line(r)}" for r in refls
                    )
                    parts.append(f"**Latest reflections:**\n\n{bullets}")

        tools_json = agent.workspace / (ident.mcp_config or "tools.json")
        skills = list_skills(tools_json)
        if skills:
            parts.append(f"**Skills (MCP servers):** {', '.join(skills)}")

        return "\n\n".join(parts)


# -- ~/.claude/CLAUDE.md install / uninstall ----------------------------------


def install_into_claude_md(
    brain_path: Path,
    claude_md_path: Path = DEFAULT_CLAUDE_MD,
) -> bool:
    """Append (or replace) a sentinel-wrapped @import block in CLAUDE.md.

    Returns True if the file was modified.
    """
    claude_md_path = Path(claude_md_path)
    claude_md_path.parent.mkdir(parents=True, exist_ok=True)

    existing = ""
    if claude_md_path.exists():
        existing = claude_md_path.read_text(encoding="utf-8")

    # Strip any existing managed block so we can re-append a fresh one.
    stripped = _SENTINEL_RE.sub("", existing).rstrip()

    import_line = f"@{_display_path(brain_path)}"
    block = f"{BEGIN_SENTINEL}\n{import_line}\n{END_SENTINEL}\n"

    if stripped:
        new_content = stripped + "\n\n" + block
    else:
        new_content = block

    if new_content == existing:
        return False

    claude_md_path.write_text(new_content, encoding="utf-8")
    return True


def uninstall_from_claude_md(
    claude_md_path: Path = DEFAULT_CLAUDE_MD,
) -> bool:
    """Remove the sentinel-wrapped block from CLAUDE.md. Returns True if changed."""
    claude_md_path = Path(claude_md_path)
    if not claude_md_path.exists():
        return False
    original = claude_md_path.read_text(encoding="utf-8")
    new_content = _SENTINEL_RE.sub("", original).rstrip()
    if new_content:
        new_content += "\n"
    if new_content == original:
        return False
    claude_md_path.write_text(new_content, encoding="utf-8")
    return True


def brain_status(
    brain_path: Path,
    claude_md_path: Path = DEFAULT_CLAUDE_MD,
) -> dict:
    """Return a dict describing current install state + file stats."""
    brain_path = Path(brain_path)
    claude_md_path = Path(claude_md_path)

    installed = False
    if claude_md_path.exists():
        installed = bool(_SENTINEL_RE.search(claude_md_path.read_text(encoding="utf-8")))

    info: dict = {
        "brain_path": str(brain_path),
        "brain_exists": brain_path.exists(),
        "claude_md_path": str(claude_md_path),
        "installed": installed,
    }
    if brain_path.exists():
        stat = brain_path.stat()
        info["size_bytes"] = stat.st_size
        info["last_sync"] = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return info


# -- Utilities ----------------------------------------------------------------


def _read_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        return stripped
    return ""


def _word_clip(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).rstrip(",;:") + "…"


def _top_list_items(markdown: str, n: int) -> str:
    """Pick the first n list items (ordered or unordered) from markdown."""
    items: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if re.match(r"^(\d+\.|[-*])\s+", stripped):
            items.append(stripped)
            if len(items) >= n:
                break
    if not items:
        return _word_clip(markdown, 120)
    return "\n".join(items)


def _display_path(path: Path) -> str:
    """Return `~/foo/bar` if path is under $HOME, else str(path)."""
    try:
        rel = path.resolve().relative_to(Path.home().resolve())
        return f"~/{rel}"
    except ValueError:
        return str(path)


def _try_lock(lock_path: Path) -> int | None:
    """Acquire a non-blocking exclusive lock. Returns fd or None if busy."""
    try:
        import fcntl
    except ImportError:
        return -1  # non-POSIX: skip locking, still return truthy
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _release_lock(fd: int, lock_path: Path) -> None:
    if fd < 0:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        lock_path.unlink()
    except OSError:
        pass
