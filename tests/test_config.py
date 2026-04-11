"""Tests for configuration loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from claude_daemon.core.config import DaemonConfig


def test_default_config():
    """Test that default config loads without errors."""
    config = DaemonConfig()
    assert config.log_level == "INFO"
    assert config.max_concurrent_sessions == 3
    assert config.max_budget_per_message == 0.50
    assert config.claude_binary == "claude"
    assert config.permission_mode == "auto"
    assert config.daily_log_enabled is True
    assert config.dream_enabled is True


def test_config_from_yaml(tmp_path: Path):
    """Test loading config from a YAML file."""
    cfg = {
        "daemon": {"log_level": "DEBUG"},
        "claude": {"binary": "/usr/bin/claude", "max_concurrent": 5, "model": "opus"},
        "memory": {"compaction_threshold": 10000, "daily_log": False},
        "scheduler": {"update_cron": "0 2 * * *", "heartbeat_interval": 600},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    config = DaemonConfig.load(cfg_path)
    assert config.log_level == "DEBUG"
    assert config.claude_binary == "/usr/bin/claude"
    assert config.max_concurrent_sessions == 5
    assert config.default_model == "opus"
    assert config.compaction_threshold == 10000
    assert config.daily_log_enabled is False
    assert config.update_cron == "0 2 * * *"
    assert config.heartbeat_interval == 600


def test_config_env_override(monkeypatch: pytest.MonkeyPatch):
    """Test that environment variables override config."""
    monkeypatch.setenv("CLAUDE_DAEMON_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token_123")

    config = DaemonConfig.load()
    assert config.log_level == "ERROR"
    assert config.telegram_token == "test_token_123"


def test_config_derived_paths():
    """Test derived path properties."""
    config = DaemonConfig()
    assert config.log_dir == config.data_dir / "logs"
    assert config.memory_dir == config.data_dir / "memory"
    assert config.db_path == config.data_dir / "daemon.db"
