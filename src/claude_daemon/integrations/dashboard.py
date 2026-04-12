"""DashboardHub - WebSocket event broadcaster for the live agent dashboard.

Broadcasts real-time events to connected browser clients:
- agent_status: agent starts/finishes processing (busy/idle)
- stream_delta: text chunk from an active agent session
- task_update: spawned task status change
- metrics_tick: periodic per-agent cost/token summary
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp import web

log = logging.getLogger(__name__)


class DashboardHub:
    """Broadcasts agent events to connected WebSocket clients."""

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._last_metrics_tick: float = 0

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, event: dict[str, Any]) -> None:
        """Send an event to all connected WebSocket clients."""
        if not self._clients:
            return

        dead: set[web.WebSocketResponse] = set()
        for ws in self._clients:
            try:
                await ws.send_json(event)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle a new WebSocket connection from the dashboard."""
        from aiohttp import web as aio_web

        ws = aio_web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("Dashboard client connected (%d total)", len(self._clients))

        try:
            async for _ in ws:
                pass  # Client messages ignored for now
        finally:
            self._clients.discard(ws)
            log.info("Dashboard client disconnected (%d remaining)", len(self._clients))

        return ws

    # -- Convenience event emitters --

    async def agent_busy(self, agent_name: str, prompt: str = "") -> None:
        await self.broadcast({
            "type": "agent_status",
            "agent": agent_name,
            "status": "busy",
            "prompt": prompt[:200],
            "ts": time.time(),
        })

    async def agent_idle(
        self, agent_name: str, cost: float = 0, duration_ms: int = 0,
    ) -> None:
        await self.broadcast({
            "type": "agent_status",
            "agent": agent_name,
            "status": "idle",
            "cost": cost,
            "duration_ms": duration_ms,
            "ts": time.time(),
        })

    async def stream_delta(self, agent_name: str, text: str) -> None:
        await self.broadcast({
            "type": "stream_delta",
            "agent": agent_name,
            "text": text,
            "ts": time.time(),
        })

    async def task_update(
        self, task_id: str, agent_name: str, status: str,
        result: str = "", cost: float = 0,
    ) -> None:
        await self.broadcast({
            "type": "task_update",
            "task_id": task_id,
            "agent": agent_name,
            "status": status,
            "result": result[:500],
            "cost": cost,
            "ts": time.time(),
        })

    async def auto_parallel(self, agent_name: str, session_id: str) -> None:
        await self.broadcast({
            "type": "auto_parallel",
            "agent": agent_name,
            "session_id": session_id[:12],
            "ts": time.time(),
        })

    async def metrics_tick(self, metrics: list[dict]) -> None:
        """Periodic summary of per-agent metrics."""
        now = time.time()
        if now - self._last_metrics_tick < 30:
            return  # Throttle to every 30s
        self._last_metrics_tick = now
        await self.broadcast({
            "type": "metrics_tick",
            "metrics": metrics,
            "ts": now,
        })
