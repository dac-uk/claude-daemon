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
        """Send an event to all connected WebSocket clients concurrently."""
        if not self._clients:
            return

        async def _send(ws: web.WebSocketResponse) -> web.WebSocketResponse | None:
            try:
                await ws.send_json(event)
                return None
            except Exception:
                return ws

        results = await asyncio.gather(
            *(_send(ws) for ws in self._clients),
            return_exceptions=True,
        )
        dead = {r for r in results if isinstance(r, web.WebSocketResponse)}
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

    async def task_created(
        self, task_id: str, agent_name: str, prompt: str = "",
    ) -> None:
        """Broadcast when a new task enters the queue (pre-dispatch)."""
        await self.broadcast({
            "type": "task_created",
            "task_id": task_id,
            "agent": agent_name,
            "prompt": prompt[:200],
            "status": "pending",
            "ts": time.time(),
        })

    async def task_cancelled(self, task_id: str, agent_name: str = "") -> None:
        """Broadcast when a task is cancelled."""
        await self.broadcast({
            "type": "task_cancelled",
            "task_id": task_id,
            "agent": agent_name,
            "status": "cancelled",
            "ts": time.time(),
        })

    async def goal_update(
        self, goal_id: int, title: str, status: str,
    ) -> None:
        """Broadcast when a goal is created or updated."""
        await self.broadcast({
            "type": "goal_update",
            "goal_id": goal_id,
            "title": title,
            "status": status,
            "ts": time.time(),
        })

    async def goal_progress(
        self, goal_id: int, title: str, pct: float,
    ) -> None:
        """Broadcast goal progress changes."""
        await self.broadcast({
            "type": "goal_progress",
            "goal_id": goal_id,
            "title": title,
            "pct": pct,
            "ts": time.time(),
        })

    async def approval_requested(
        self, approval_id: int, task_id: str, reason: str = "",
    ) -> None:
        """Broadcast when a task requires approval."""
        await self.broadcast({
            "type": "approval_requested",
            "approval_id": approval_id,
            "task_id": task_id,
            "reason": reason,
            "ts": time.time(),
        })

    async def approval_resolved(
        self, approval_id: int, task_id: str, outcome: str,
        approver: str = "",
    ) -> None:
        """Broadcast when an approval is approved or rejected."""
        await self.broadcast({
            "type": "approval_resolved",
            "approval_id": approval_id,
            "task_id": task_id,
            "outcome": outcome,
            "approver": approver,
            "ts": time.time(),
        })

    async def budget_update(
        self,
        budget_id: int,
        scope: str,
        scope_value: str | None,
        current_spend: float,
        limit_usd: float,
    ) -> None:
        """Broadcast when a budget's spend changes."""
        await self.broadcast({
            "type": "budget_update",
            "budget_id": budget_id,
            "scope": scope,
            "scope_value": scope_value,
            "current_spend": current_spend,
            "limit_usd": limit_usd,
            "ts": time.time(),
        })

    async def budget_exceeded(
        self, budget_id: int, scope: str, scope_value: str | None,
        current_spend: float, limit_usd: float,
    ) -> None:
        """Broadcast when a task is rejected due to budget exhaustion."""
        await self.broadcast({
            "type": "budget_exceeded",
            "budget_id": budget_id,
            "scope": scope,
            "scope_value": scope_value,
            "current_spend": current_spend,
            "limit_usd": limit_usd,
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
