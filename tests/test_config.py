"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from claude_daemon.core.config import DaemonConfig


def test_default_config():
    config = DaemonConfig()
    assert config.log_level == "INFO"
    assert config.max_concurrent_sessions == 3
    assert config.max_budget_per_message == 0.50
    assert config.claude_binary == "claude"
    assert config.permission_mode == "auto"
    assert config.daily_log_enabled is True
    assert config.dream_enabled is True
    assert config.streaming_enabled is True
    assert config.process_timeout == 300
    assert config.max_context_chars == 5000
    assert config.max_memory_chars == 3000
    assert config.log_retention_days == 30
    assert config.self_improve is True
    assert config.rate_limit_per_user == 20


def test_config_from_yaml(tmp_path: Path):
    cfg = {
        "daemon": {"log_level": "DEBUG", "rate_limit_per_user": 5},
        "claude": {
            "binary": "/usr/bin/claude",
            "max_concurrent": 5,
            "model": "opus",
            "process_timeout": 600,
            "mcp_config": "/path/to/mcp.json",
            "streaming": False,
        },
        "memory": {
            "compaction_threshold": 10000,
            "daily_log": False,
            "max_context_chars": 8000,
            "log_retention_days": 14,
            "self_improve": False,
        },
        "scheduler": {"update_cron": "0 2 * * *", "heartbeat_interval": 600},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    config = DaemonConfig.load(cfg_path)
    assert config.log_level == "DEBUG"
    assert config.claude_binary == "/usr/bin/claude"
    assert config.max_concurrent_sessions == 5
    assert config.default_model == "opus"
    assert config.process_timeout == 600
    assert config.mcp_config == "/path/to/mcp.json"
    assert config.streaming_enabled is False
    assert config.compaction_threshold == 10000
    assert config.daily_log_enabled is False
    assert config.max_context_chars == 8000
    assert config.log_retention_days == 14
    assert config.self_improve is False
    assert config.rate_limit_per_user == 5


def test_config_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_DAEMON_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token_123")

    config = DaemonConfig.load()
    assert config.log_level == "ERROR"
    assert config.telegram_token == "test_token_123"


def test_config_derived_paths():
    config = DaemonConfig()
    assert config.log_dir == config.data_dir / "logs"
    assert config.memory_dir == config.data_dir / "memory"
    assert config.db_path == config.data_dir / "daemon.db"
    assert config.soul_path == config.data_dir / "SOUL.md"
    assert config.reflections_path == config.data_dir / "REFLECTIONS.md"
