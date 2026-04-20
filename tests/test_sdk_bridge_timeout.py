"""Regression tests for SDK bridge idle timeout behaviour.

After 2026-04-18 the whole-stream `process_timeout` was replaced with a
per-event idle timeout (`sdk_bridge_idle_timeout_ms`). A silent bridge
must fail over quickly rather than sitting quiet for 5 minutes; a bridge
that keeps emitting events must not be killed just because the total
stream is long.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from claude_daemon.core.process import ClaudeResponse
from claude_daemon.core.sdk_bridge import SDKBridgeManager


def _config(idle_ms: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        sdk_bridge_idle_timeout_ms=idle_ms,
        process_timeout=300,  # not used by the stream path anymore, but set for safety
        sdk_bridge_node_path="node",
    )


@pytest.mark.asyncio
async def test_silent_bridge_fails_fast_on_idle_timeout():
    """No events for idle_timeout seconds → yield error, don't wait 300s."""
    mgr = SDKBridgeManager(_config(idle_ms=50))
    mgr._send_command = AsyncMock()  # don't actually try to write

    responses = []
    start = asyncio.get_event_loop().time()
    async for item in mgr.stream_message("albert", "hi"):
        responses.append(item)
    elapsed = asyncio.get_event_loop().time() - start

    # Should surface a timeout error, not raise
    assert len(responses) == 1
    resp = responses[0]
    assert isinstance(resp, ClaudeResponse)
    assert resp.is_error
    assert "idle timeout" in resp.result.lower()
    # ~0.05s idle + scheduling overhead — well under 1s
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_events_reset_idle_timer():
    """Continuous events keep the stream alive past the idle window."""
    mgr = SDKBridgeManager(_config(idle_ms=80))
    mgr._send_command = AsyncMock()

    # Feed events at 40ms cadence (well under 80ms idle) for ~0.2s, then result
    async def feeder():
        # Wait until the stream has registered its queue
        for _ in range(10):
            if mgr._streams:
                break
            await asyncio.sleep(0.005)
        assert mgr._streams, "stream_message never registered a queue"
        queue = next(iter(mgr._streams.values()))
        for i in range(5):
            await queue.put({"event": "text", "text": f"chunk{i} "})
            await asyncio.sleep(0.04)
        await queue.put({
            "event": "result", "sessionId": "s1", "result": "done",
            "cost": 0.01, "inputTokens": 1, "outputTokens": 1, "durationMs": 200,
        })

    feeder_task = asyncio.create_task(feeder())
    chunks = []
    final: ClaudeResponse | None = None
    async for item in mgr.stream_message("albert", "hi"):
        if isinstance(item, ClaudeResponse):
            final = item
        else:
            chunks.append(item)
    await feeder_task

    assert final is not None
    assert not final.is_error, f"unexpected error: {final.result}"
    assert final.result == "done"
    assert len(chunks) == 5


def test_config_exposes_idle_timeout_field():
    """Config field exists so callers can tune it via daemon.yaml."""
    from claude_daemon.core.config import DaemonConfig
    cfg = DaemonConfig()
    assert hasattr(cfg, "sdk_bridge_idle_timeout_ms")
    assert cfg.sdk_bridge_idle_timeout_ms == 300_000
