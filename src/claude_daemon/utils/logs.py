"""Log-tail helpers for the Alerts dashboard view."""

from __future__ import annotations

import os
import re
from pathlib import Path

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_INDEX = {lvl: i for i, lvl in enumerate(LEVELS)}

# Matches the formatter in utils/logging.py:
#   "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
# e.g. "2026-04-18 12:34:56 [WARNING] claude_daemon.foo: message"
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"\[(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\]\s+"
    r"(?P<logger>[^:]+):\s*(?P<message>.*)$"
)


def _bounded_tail_bytes(path: Path, max_bytes: int) -> bytes:
    """Read up to max_bytes from the end of path without loading the whole file."""
    size = path.stat().st_size
    read = min(max_bytes, size)
    with path.open("rb") as f:
        f.seek(size - read)
        return f.read(read)


def tail_log(
    path: Path,
    lines: int = 200,
    min_level: str = "WARNING",
    since: str | None = None,
) -> list[dict]:
    """Return at most `lines` recent log entries from `path`.

    - Keeps only entries at `min_level` or above.
    - If `since` (ISO-ish 'YYYY-MM-DD HH:MM:SS') is given, drops earlier entries.
    - Multi-line content (tracebacks, continued messages) is attached to the
      previous parsed entry under a 'traceback' key.
    - Returns chronological order (oldest first).
    """
    if not path.exists():
        return []

    min_idx = _LEVEL_INDEX.get(min_level.upper(), _LEVEL_INDEX["WARNING"])

    # Read ~256 bytes per expected line; cap at 2MB.
    budget = min(2 * 1024 * 1024, max(64 * 1024, lines * 400))
    try:
        raw = _bounded_tail_bytes(path, budget)
    except OSError:
        return []

    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return []

    # If we started mid-line, drop the partial first line.
    parts = text.splitlines()
    if parts and not _LINE_RE.match(parts[0]) and len(parts) > 1:
        parts = parts[1:]

    entries: list[dict] = []
    for raw_line in parts:
        m = _LINE_RE.match(raw_line)
        if m:
            level = m.group("level")
            if _LEVEL_INDEX[level] < min_idx:
                continue
            entries.append({
                "timestamp": m.group("ts"),
                "level": level,
                "logger": m.group("logger"),
                "message": m.group("message"),
                "traceback": [],
            })
        else:
            # Continuation line (traceback, indented detail). Attach to last
            # entry only if that entry was kept.
            if entries:
                entries[-1]["traceback"].append(raw_line)

    if since:
        entries = [e for e in entries if e["timestamp"] >= since]

    if len(entries) > lines:
        entries = entries[-lines:]

    # Collapse empty traceback lists for cleaner JSON output.
    for e in entries:
        if not e["traceback"]:
            e.pop("traceback")
        else:
            e["traceback"] = "\n".join(e["traceback"]).rstrip()

    return entries


def default_log_path() -> Path:
    """Return the path where setup_logging() writes daemon.log."""
    from claude_daemon.utils.paths import log_dir
    return log_dir() / "daemon.log"
