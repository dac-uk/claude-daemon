"""Regression tests for DashboardHub.broadcast (Phase 10).

The original bug: `broadcast()` used `isinstance(r, web.WebSocketResponse)` to
detect dead clients, but `web` was only imported under `TYPE_CHECKING` — so the
call raised `NameError` at runtime whenever at least one client was connected.
Every chat message therefore failed at `hub.agent_busy()` before the agent was
ever invoked. No existing test caught this because every caller mocked `hub`
with a plain `MagicMock`.

These tests exercise `broadcast()` directly against `WebSocketResponse`-like
objects so the runtime path is covered.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from claude_daemon.integrations.dashboard import DashboardHub


@pytest.mark.asyncio
async def test_broadcast_with_connected_client_succeeds():
    """With a live client, broadcast sends the event and does not raise."""
    hub = DashboardHub()
    ws = MagicMock(spec=web.WebSocketResponse)
    ws.send_json = AsyncMock()
    hub._clients.add(ws)

    await hub.agent_busy("albert", "hello")

    assert ws.send_json.await_count == 1
    payload = ws.send_json.await_args.args[0]
    assert payload["type"] == "agent_status"
    assert payload["agent"] == "albert"
    assert payload["status"] == "busy"
    # Client must still be tracked — send succeeded.
    assert ws in hub._clients


@pytest.mark.asyncio
async def test_broadcast_removes_dead_client():
    """A client whose send_json raises is evicted from the clients set."""
    hub = DashboardHub()
    dead_ws = MagicMock(spec=web.WebSocketResponse)
    dead_ws.send_json = AsyncMock(side_effect=ConnectionResetError("peer gone"))
    live_ws = MagicMock(spec=web.WebSocketResponse)
    live_ws.send_json = AsyncMock()
    hub._clients.update({dead_ws, live_ws})

    await hub.broadcast({"type": "test", "v": 1})

    assert dead_ws not in hub._clients
    assert live_ws in hub._clients


@pytest.mark.asyncio
async def test_broadcast_with_no_clients_noops():
    """Empty clients set is a no-op — no raise, no iteration."""
    hub = DashboardHub()
    assert hub._clients == set()

    # Should return cleanly.
    await hub.broadcast({"type": "test"})
    await hub.agent_idle("albert", cost=0.0, duration_ms=0)

    assert hub._clients == set()
