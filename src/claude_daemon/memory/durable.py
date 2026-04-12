"""DurableMemory - markdown-based persistent memory with SOUL.md support.

File layout:
- MEMORY.md: persistent cross-session knowledge (updated by REM sleep)
- SOUL.md: agent personality, identity, values (user-editable)
- REFLECTIONS.md: self-improvement learnings (updated by reflexion system)
- memory/YYYY-MM-DD.md: daily activity logs
- memory/archive/: versioned snapshots of MEMORY.md before updates
"""

from __future__ import annotations

import fcntl
import logging
import shutil
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_SOUL = """\
# Soul

## Identity
I am Claude Daemon, a persistent AI assistant running as a background service.
I maintain continuity across conversations and platforms.

## Communication Style
- Direct and concise, but warm
- I remember context from previous conversations
- I proactively surface relevant memories when helpful
- I adapt my tone to the platform (casual on Telegram, structured on Discord)

## Values
- Reliability: I follow through on tasks and remember commitments
- Honesty: I acknowledge when I don't know or when I made a mistake
- Efficiency: I respect the user's time and attention
- Growth: I learn from each interaction to become more effective

## Boundaries
- I don't pretend to remember things I don't actually have in memory
- I flag when my memory context seems incomplete or outdated
- I ask for clarification rather than guessing on important decisions
"""


class DurableMemory:
    """Manages durable markdown memory files with SOUL.md personality system."""

    def __init__(self, memory_dir: Path) -> None:
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        (self.memory_dir / "archive").mkdir(exist_ok=True)
        self._lock_path = self.memory_dir / ".memory.lock"

    @contextmanager
    def _file_lock(self):
        """Acquire an exclusive file lock to prevent concurrent MEMORY.md writes."""
        lock_fd = open(self._lock_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    @property
    def memory_file(self) -> Path:
        return self.memory_dir / "MEMORY.md"

    @property
    def soul_file(self) -> Path:
        return self.memory_dir / "SOUL.md"

    @property
    def reflections_file(self) -> Path:
        return self.memory_dir / "REFLECTIONS.md"

    def _daily_log_path(self, d: date | None = None) -> Path:
        d = d or date.today()
        return self.memory_dir / f"{d.isoformat()}.md"

    # -- MEMORY.md --

    def read_memory(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text()
        return ""

    def update_memory(self, content: str, validate: bool = True) -> bool:
        """Write new MEMORY.md content with file locking. Returns True if written, False if rejected."""
        with self._file_lock():
            if validate:
                old = self.read_memory()
                if old and content:
                    if len(content) < len(old) * 0.3:
                        log.warning(
                            "Memory update rejected: new (%d chars) is <30%% of old (%d chars). "
                            "This looks like data loss. Archive preserved.",
                            len(content), len(old),
                        )
                        return False
                    old_lines = set(old.strip().split("\n"))
                    new_lines = set(content.strip().split("\n"))
                    added = len(new_lines - old_lines)
                    removed = len(old_lines - new_lines)
                    if added or removed:
                        self.append_daily_log(
                            f"MEMORY.md updated: +{added} lines, -{removed} lines "
                            f"({len(old)} -> {len(content)} chars)"
                        )
                elif not content.strip():
                    log.warning("Memory update rejected: empty content")
                    return False

            self.memory_file.write_text(content)
            log.info("Updated MEMORY.md (%d chars)", len(content))
            return True

    def archive_memory(self) -> None:
        """Create a timestamped backup of MEMORY.md before overwriting."""
        if not self.memory_file.exists():
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        archive = self.memory_dir / "archive" / f"MEMORY_{ts}.md"
        shutil.copy2(self.memory_file, archive)
        log.info("Archived MEMORY.md to %s", archive.name)

    # -- SOUL.md --

    def read_soul(self) -> str:
        if self.soul_file.exists():
            return self.soul_file.read_text()
        return DEFAULT_SOUL

    def ensure_soul(self) -> None:
        """Create default SOUL.md if it doesn't exist."""
        if not self.soul_file.exists():
            self.soul_file.write_text(DEFAULT_SOUL)
            log.info("Created default SOUL.md")

    # -- REFLECTIONS.md --

    def read_reflections(self) -> str:
        if self.reflections_file.exists():
            return self.reflections_file.read_text()
        return ""

    def update_reflections(self, content: str) -> None:
        self.reflections_file.write_text(content)
        log.info("Updated REFLECTIONS.md (%d chars)", len(content))

    # -- Daily Logs --

    def append_daily_log(self, entry: str, d: date | None = None) -> None:
        path = self._daily_log_path(d)
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

        if not path.exists():
            path.write_text(f"# Daily Log - {(d or date.today()).isoformat()}\n\n")

        with open(path, "a") as f:
            f.write(f"- [{timestamp}] {entry}\n")

    def read_daily_log(self, d: date | None = None) -> str:
        path = self._daily_log_path(d)
        if path.exists():
            return path.read_text()
        return ""

    def read_recent_logs(self, days: int = 3) -> str:
        parts = []
        today = date.today()
        for i in range(days):
            d = today - timedelta(days=i)
            content = self.read_daily_log(d)
            if content:
                parts.append(content)
        return "\n---\n".join(parts)

    def cleanup_old_logs(self, retention_days: int = 30) -> int:
        """Delete daily log files older than retention_days. Returns count deleted."""
        cutoff = date.today() - timedelta(days=retention_days)
        deleted = 0
        for path in self.memory_dir.glob("????-??-??.md"):
            try:
                file_date = date.fromisoformat(path.stem)
                if file_date < cutoff:
                    path.unlink()
                    deleted += 1
            except ValueError:
                continue
        if deleted:
            log.info("Cleaned up %d daily logs older than %d days", deleted, retention_days)
        return deleted

    # -- Context Building --

    def get_context_block(self, recent_days: int = 3, max_chars: int = 5000) -> str:
        """Build a context string from all durable sources."""
        blocks = []

        # Soul/identity (always included, compact)
        soul = self.read_soul()
        if soul:
            blocks.append(f"## Your Identity\n{soul[:800]}")

        # Persistent memory
        memory = self.read_memory()
        if memory:
            blocks.append(f"## Persistent Memory\n{memory}")

        # Self-reflections (compact)
        reflections = self.read_reflections()
        if reflections:
            blocks.append(f"## Self-Reflections\n{reflections[:500]}")

        # Recent activity
        recent = self.read_recent_logs(recent_days)
        if recent:
            if len(recent) > 1500:
                recent = recent[-1500:]
            blocks.append(f"## Recent Activity\n{recent}")

        context = "\n\n".join(blocks)

        # Enforce total size limit
        if len(context) > max_chars:
            context = context[:max_chars]

        return context
