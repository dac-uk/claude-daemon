"""Tests for pre-warming hardening: concurrency semaphore + is_alive guard."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_daemon.agents.registry import AgentRegistry
from claude_daemon.core.config import DaemonConfig


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


@pytest.fixture
def registry(agents_dir: Path) -> AgentRegistry:
    reg = AgentRegistry(agents_dir)
    for name in ("johnny", "albert", "luna", "max"):
        reg.create_agent(name, role="test")
    return reg


# ── Config Tests ─────────────────────────────────────────────────


class TestPrewarmConfig:
    def test_default_concurrency_is_4(self):
        cfg = DaemonConfig()
        assert cfg.sdk_prewarm_concurrency == 4

    def test_config_from_yaml(self, tmp_path):
        import yaml
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump({"claude": {"sdk_prewarm_concurrency": 2}}))
        cfg = DaemonConfig.load(cfg_path)
        assert cfg.sdk_prewarm_concurrency == 2


# ── Bridge is_alive Tests ────────────────────────────────────────


class TestBridgeIsAlive:
    def test_is_alive_when_running(self):
        from claude_daemon.core.sdk_bridge import SDKBridgeManager
        bridge = SDKBridgeManager(DaemonConfig())
        bridge._process = MagicMock()
        bridge._process.returncode = None
        assert bridge.is_alive is True

    def test_not_alive_when_exited(self):
        from claude_daemon.core.sdk_bridge import SDKBridgeManager
        bridge = SDKBridgeManager(DaemonConfig())
        bridge._process = MagicMock()
        bridge._process.returncode = 1
        assert bridge.is_alive is False

    def test_not_alive_when_no_process(self):
        from claude_daemon.core.sdk_bridge import SDKBridgeManager
        bridge = SDKBridgeManager(DaemonConfig())
        assert bridge.is_alive is False


# ── Pre-warm Concurrency Tests ───────────────────────────────────


class TestPrewarmConcurrency:
    @pytest.mark.asyncio
    async def test_concurrency_limited(self, registry, tmp_path):
        """Verify at most sdk_prewarm_concurrency tasks run simultaneously."""
        from claude_daemon.core.daemon import ClaudeDaemon

        config = DaemonConfig(data_dir=tmp_path, sdk_prewarm_concurrency=2)
        daemon = ClaudeDaemon.__new__(ClaudeDaemon)
        daemon.config = config
        daemon.agent_registry = registry

        peak_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def mock_ensure(agent_name, model, **kwargs):
            nonlocal peak_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > peak_concurrent:
                    peak_concurrent = current_concurrent
            await asyncio.sleep(0.05)
            async with lock:
                current_concurrent -= 1
            return "sess_id"

        bridge = MagicMock()
        bridge.is_alive = True
        pm = MagicMock()
        pm._sdk_bridge = bridge
        pm.ensure_agent_session = AsyncMock(side_effect=mock_ensure)
        daemon.process_manager = pm

        await daemon._precreate_agent_sessions()
        assert peak_concurrent <= 2, f"Peak was {peak_concurrent}, expected <= 2"
        # johnny+albert are priority → 2 models each; luna+max → 1 each = 6 total
        assert pm.ensure_agent_session.call_count == 6

    @pytest.mark.asyncio
    async def test_skips_when_bridge_not_alive(self, registry, tmp_path):
        """Pre-warm should return immediately when bridge is not alive."""
        from claude_daemon.core.daemon import ClaudeDaemon

        config = DaemonConfig(data_dir=tmp_path)
        daemon = ClaudeDaemon.__new__(ClaudeDaemon)
        daemon.config = config
        daemon.agent_registry = registry

        bridge = MagicMock()
        bridge.is_alive = False
        pm = MagicMock()
        pm._sdk_bridge = bridge
        pm.ensure_agent_session = AsyncMock()
        daemon.process_manager = pm

        await daemon._precreate_agent_sessions()
        pm.ensure_agent_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_bridge(self, registry, tmp_path):
        """Pre-warm should return immediately when bridge is None."""
        from claude_daemon.core.daemon import ClaudeDaemon

        config = DaemonConfig(data_dir=tmp_path)
        daemon = ClaudeDaemon.__new__(ClaudeDaemon)
        daemon.config = config
        daemon.agent_registry = registry

        pm = MagicMock()
        pm._sdk_bridge = None
        daemon.process_manager = pm

        await daemon._precreate_agent_sessions()
        pm.ensure_agent_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_doesnt_block_others(self, registry, tmp_path):
        """One agent failing should not prevent others from warming."""
        from claude_daemon.core.daemon import ClaudeDaemon

        config = DaemonConfig(data_dir=tmp_path, sdk_prewarm_concurrency=4)
        daemon = ClaudeDaemon.__new__(ClaudeDaemon)
        daemon.config = config
        daemon.agent_registry = registry

        call_count = 0

        async def mock_ensure(agent_name, model, **kwargs):
            nonlocal call_count
            call_count += 1
            if agent_name == "albert":
                raise RuntimeError("MCP init failed")
            return "sess_id"

        bridge = MagicMock()
        bridge.is_alive = True
        pm = MagicMock()
        pm._sdk_bridge = bridge
        pm.ensure_agent_session = AsyncMock(side_effect=mock_ensure)
        daemon.process_manager = pm

        await daemon._precreate_agent_sessions()
        assert call_count == 6  # all agents attempted despite albert's failure (6 = priority models)
