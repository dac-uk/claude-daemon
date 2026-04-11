"""Structured logging setup for claude-daemon."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> logging.Logger:
    """Configure logging to stderr and optionally to a file.

    Returns the root 'claude_daemon' logger.
    """
    logger = logging.getLogger("claude_daemon")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Always log to stderr
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    logger.addHandler(stderr_handler)

    # Optionally log to file
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "daemon.log")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger
