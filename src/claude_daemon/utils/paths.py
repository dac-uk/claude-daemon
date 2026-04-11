"""Platform-aware path resolution for claude-daemon data and config."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def config_dir() -> Path:
    """Return the configuration directory, respecting XDG on Linux."""
    env = os.environ.get("CLAUDE_DAEMON_DATA_DIR")
    if env:
        return Path(env)
    if _is_linux():
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            return Path(xdg) / "claude-daemon"
    return Path.home() / ".config" / "claude-daemon"


def data_dir() -> Path:
    """Return the data directory (same as config_dir by default)."""
    return config_dir()


def log_dir() -> Path:
    """Return the log directory."""
    return config_dir() / "logs"


def memory_dir() -> Path:
    """Return the directory for durable memory files."""
    return config_dir() / "memory"


def db_path() -> Path:
    """Return the path to the SQLite database."""
    return config_dir() / "daemon.db"


def pid_path() -> Path:
    """Return the path to the PID file."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "claude-daemon.pid"
    return config_dir() / "daemon.pid"


def ensure_dirs() -> None:
    """Create all required directories if they don't exist."""
    for d in [config_dir(), log_dir(), memory_dir()]:
        d.mkdir(parents=True, exist_ok=True)
