"""Built-in job definitions for the scheduler.

Job implementations live in SchedulerEngine for direct access to the daemon.
This module provides job metadata and configuration helpers.
"""

from __future__ import annotations

BUILTIN_JOBS = {
    "auto_update": {
        "name": "Auto-update Claude Code",
        "description": "Check for and install Claude Code updates",
        "default_cron": "0 3 * * *",
    },
    "memory_compaction": {
        "name": "Memory compaction",
        "description": "Summarize active sessions and write daily logs",
        "default_cron": "0 4 * * *",
    },
    "auto_dream": {
        "name": "Auto-dream",
        "description": "Weekly memory consolidation into MEMORY.md",
        "default_cron": "0 5 * * 0",
    },
    "session_cleanup": {
        "name": "Session cleanup",
        "description": "Archive expired conversations",
        "default_interval": "6h",
    },
    "heartbeat": {
        "name": "Heartbeat",
        "description": "Periodic health status log",
        "default_interval": "1800s",
    },
}
