"""DurableMemory - markdown-based persistent memory.

Follows the OpenClaw pattern:
- MEMORY.md: persistent cross-session notes, preferences, facts
- memory/YYYY-MM-DD.md: daily activity logs
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class DurableMemory:
    """Manages durable markdown memory files."""

    def __init__(self, memory_dir: Path) -> None:
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    @property
    def memory_file(self) -> Path:
        return self.memory_dir / "MEMORY.md"

    def _daily_log_path(self, d: date | None = None) -> Path:
        d = d or date.today()
        return self.memory_dir / f"{d.isoformat()}.md"

    # -- MEMORY.md --

    def read_memory(self) -> str:
        """Read the persistent MEMORY.md file."""
        if self.memory_file.exists():
            return self.memory_file.read_text()
        return ""

    def update_memory(self, content: str) -> None:
        """Overwrite MEMORY.md with new content. Used by compactor/dream."""
        self.memory_file.write_text(content)
        log.info("Updated MEMORY.md (%d chars)", len(content))

    def append_memory(self, entry: str) -> None:
        """Append a new entry to MEMORY.md."""
        current = self.read_memory()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        new_entry = f"\n## [{timestamp}]\n{entry}\n"

        if current:
            content = current + new_entry
        else:
            content = f"# Persistent Memory\n{new_entry}"

        # Cap at 2000 chars to keep context injection efficient
        if len(content) > 2000:
            lines = content.split("\n")
            # Keep the header and trim old entries from the middle
            content = "\n".join(lines[:2] + lines[-(len(lines) // 2):])
            if len(content) > 2000:
                content = content[-2000:]

        self.update_memory(content)

    # -- Daily Logs --

    def append_daily_log(self, entry: str, d: date | None = None) -> None:
        """Append an entry to today's daily log file."""
        path = self._daily_log_path(d)
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

        if not path.exists():
            path.write_text(f"# Daily Log - {(d or date.today()).isoformat()}\n\n")

        with open(path, "a") as f:
            f.write(f"- [{timestamp}] {entry}\n")

    def read_daily_log(self, d: date | None = None) -> str:
        """Read a specific day's log."""
        path = self._daily_log_path(d)
        if path.exists():
            return path.read_text()
        return ""

    def read_recent_logs(self, days: int = 3) -> str:
        """Read the last N days of daily logs, concatenated."""
        parts = []
        today = date.today()
        for i in range(days):
            d = today - timedelta(days=i)
            content = self.read_daily_log(d)
            if content:
                parts.append(content)

        return "\n---\n".join(parts)

    # -- Context Building --

    def get_context_block(self, recent_days: int = 3) -> str:
        """Build a context string from MEMORY.md + recent daily logs.

        This is injected into Claude via --append-system-prompt.
        """
        blocks = []

        # Persistent memory
        memory = self.read_memory()
        if memory:
            blocks.append(f"## Persistent Memory\n{memory}")

        # Recent activity
        recent = self.read_recent_logs(recent_days)
        if recent:
            # Truncate to keep context reasonable
            if len(recent) > 2000:
                recent = recent[-2000:]
            blocks.append(f"## Recent Activity\n{recent}")

        return "\n\n".join(blocks)
