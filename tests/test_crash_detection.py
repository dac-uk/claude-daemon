"""Regression tests for _detect_crash_restart sentinel handling and
append_daily_log durability.

Context: `claude-daemon update-stop` escalates SIGTERM → SIGKILL, which
skips the "Daemon stopped gracefully." marker write. Without a sentinel
file, every operator-initiated update trips the crash-detection warning
at daemon.py:_detect_crash_restart. These tests guard against:

- regression A: sentinel present → no warning, sentinel deleted, success
  line appended
- regression B: no sentinel + unbalanced markers → warning still fires
- regression C: append_daily_log flushes bytes to disk before returning
  (so a SIGKILL racing the graceful-stop marker can't lose it to the
  page cache).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claude_daemon.core import daemon as daemon_module
from claude_daemon.memory.durable import DurableMemory


class _StubDaemon:
    """Minimal stub with the attributes _detect_crash_restart touches."""

    def __init__(self, durable: DurableMemory) -> None:
        self.durable = durable
        self.store = None
        self.router = None

    _detect_crash_restart = daemon_module.ClaudeDaemon._detect_crash_restart
    _alert_crash_restart = daemon_module.ClaudeDaemon._alert_crash_restart


@pytest.fixture
def durable(tmp_path: Path) -> DurableMemory:
    return DurableMemory(tmp_path / "memory")


@pytest.fixture
def sentinel_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / ".updating"

    def _sentinel():
        return path

    monkeypatch.setattr(
        "claude_daemon.utils.paths.update_sentinel_path", _sentinel
    )
    return path


async def test_sentinel_suppresses_crash_warning(
    durable: DurableMemory, sentinel_path: Path, caplog
):
    """A: sentinel present → no warning, sentinel deleted, info line appended."""
    # Unbalanced markers — would normally trip the crash warning.
    durable.append_daily_log("Daemon started")
    sentinel_path.write_text("pid=123\nts=1\n")

    d = _StubDaemon(durable)
    with caplog.at_level(logging.WARNING):
        await d._detect_crash_restart()

    assert not any(
        "did not shut down cleanly" in r.message for r in caplog.records
    )
    assert not sentinel_path.exists()
    assert "operator update" in durable.read_daily_log()


async def test_unbalanced_markers_without_sentinel_still_warn(
    durable: DurableMemory, sentinel_path: Path, caplog
):
    """B: no sentinel + unbalanced markers → warning fires (regression guard)."""
    durable.append_daily_log("Daemon started")
    assert not sentinel_path.exists()

    d = _StubDaemon(durable)
    with caplog.at_level(logging.WARNING):
        await d._detect_crash_restart()

    assert any(
        "did not shut down cleanly" in r.message for r in caplog.records
    )


def test_append_daily_log_flushes_to_disk(durable: DurableMemory):
    """C: bytes are present on disk the moment append_daily_log returns."""
    durable.append_daily_log("marker that must survive SIGKILL")

    path = durable.memory_dir / f"{_today()}.md"
    assert path.exists()
    # Read straight from disk via a fresh handle — proves no buffering.
    with open(path) as fh:
        contents = fh.read()
    assert "marker that must survive SIGKILL" in contents


def _today() -> str:
    from datetime import date
    return date.today().isoformat()
