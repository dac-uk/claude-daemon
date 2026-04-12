"""Tests for agent hot-reload file watcher."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_daemon.agents.watcher import AgentFileWatcher


def _make_mock_registry(tmp_path: Path, agent_names: list[str]):
    """Create a mock registry with real workspace dirs."""
    agents = []
    for name in agent_names:
        ws = tmp_path / name
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "IDENTITY.md").write_text(f"Name: {name}\nRole: test\nModel: sonnet\n")
        (ws / "SOUL.md").write_text(f"# Soul\nI am {name}.\n")

        agent = MagicMock()
        agent.name = name
        agent.workspace = ws
        agents.append(agent)

    registry = MagicMock()
    registry.__iter__ = lambda self: iter(agents)
    return registry, agents


def test_watcher_detects_identity_change(tmp_path: Path):
    """Watcher detects mtime change on IDENTITY.md and calls load_identity."""
    registry, agents = _make_mock_registry(tmp_path, ["albert"])
    watcher = AgentFileWatcher(registry, interval=1)
    watcher._snapshot_all()

    # Verify initial snapshot captured
    assert watcher._mtimes.get("albert:IDENTITY.md", 0) > 0

    # Modify the file (change mtime)
    time.sleep(0.05)
    id_path = agents[0].workspace / "IDENTITY.md"
    id_path.write_text("Name: albert\nRole: updated\nModel: opus\n")

    # Run a manual check
    watcher._check_all()

    # load_identity should have been called
    agents[0].load_identity.assert_called_once()


def test_watcher_ignores_unchanged_files(tmp_path: Path):
    """Watcher does not reload when no files have changed."""
    registry, agents = _make_mock_registry(tmp_path, ["luna"])
    watcher = AgentFileWatcher(registry, interval=1)
    watcher._snapshot_all()

    # Run check without changing anything
    watcher._check_all()

    # load_identity should NOT have been called
    agents[0].load_identity.assert_not_called()


def test_watcher_start_stop(tmp_path: Path):
    """Watcher creates and cancels an asyncio task."""
    import asyncio

    async def _test():
        registry, _ = _make_mock_registry(tmp_path, ["max"])
        watcher = AgentFileWatcher(registry, interval=60)
        watcher.start()
        assert watcher._task is not None
        assert not watcher._task.done()
        watcher.stop()
        # Give the task a moment to cancel
        await asyncio.sleep(0.05)
        assert watcher._task.done()

    asyncio.run(_test())


def test_watcher_handles_deleted_file(tmp_path: Path):
    """Watcher doesn't crash if a watched file is deleted."""
    registry, agents = _make_mock_registry(tmp_path, ["johnny"])
    watcher = AgentFileWatcher(registry, interval=1)
    watcher._snapshot_all()

    # Delete SOUL.md
    (agents[0].workspace / "SOUL.md").unlink()

    # Should not crash
    watcher._check_all()

    # load_identity should be called (mtime changed from non-zero to 0)
    agents[0].load_identity.assert_called_once()
