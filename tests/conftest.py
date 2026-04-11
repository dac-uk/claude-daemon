"""Shared test fixtures for claude-daemon."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for test data."""
    return tmp_path


@pytest.fixture
def config_dict() -> dict:
    """Provide a minimal configuration dictionary."""
    return {
        "daemon": {"log_level": "DEBUG"},
        "claude": {"binary": "echo", "max_concurrent": 1, "max_budget_per_message": 0.10},
        "memory": {"daily_log": True, "compaction_threshold": 1000},
        "scheduler": {"heartbeat_interval": 60},
        "integrations": {},
    }
