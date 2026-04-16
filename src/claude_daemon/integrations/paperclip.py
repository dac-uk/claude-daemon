"""Paperclip integration — bidirectional orchestration.

Two modes of operation:
1. **Polling** (outbound): daemon polls Paperclip for pending tasks
2. **Heartbeat** (inbound): Paperclip POSTs tasks to the daemon's webhook

Both modes run simultaneously. Polling is the fallback; heartbeat is
Paperclip's canonical pattern. Cost data is reported back on every
task completion so Paperclip can enforce budgets.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from claude_daemon.integrations.base import BaseIntegration, NormalizedMessage

log = logging.getLogger(__name__)

# Max retries for agent registration with Paperclip
_REGISTER_MAX_RETRIES = 3
_REGISTER_BACKOFF_BASE = 2  # seconds


class PaperclipIntegration(BaseIntegration):
    """Connects to Paperclip's REST API as an agent.

    Registers on startup, polls for pending tasks, and accepts incoming
    heartbeats via the HTTP API webhook endpoint. Reports cost data on
    every task completion.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        poll_interval: int = 5,
        task_limit: int = 5,
        startup_timeout: int = 30,
    ) -> None:
        super().__init__()
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.task_limit = task_limit
        self.startup_timeout = startup_timeout
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._registered = False

    async def start(self) -> None:
        """Register as agent and start polling."""
        self._client = httpx.AsyncClient(
            base_url=self.url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

        # Register with retry and backoff
        await self._register_with_retry()

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        log.info(
            "Paperclip integration started (polling every %ds, limit %d)",
            self.poll_interval, self.task_limit,
        )

    async def _register_with_retry(self) -> None:
        """Register as an agent with exponential backoff on failure."""
        for attempt in range(1, _REGISTER_MAX_RETRIES + 1):
            try:
                resp = await asyncio.wait_for(
                    self._client.post("/api/agents/register", json={
                        "name": "claude-daemon",
                        "type": "claude-code",
                        "capabilities": ["code", "analysis", "general"],
                    }),
                    timeout=self.startup_timeout,
                )
                if resp.status_code in (200, 201):
                    log.info("Registered with Paperclip at %s", self.url)
                    self._registered = True
                    return
                log.warning(
                    "Paperclip registration returned %d (attempt %d/%d): %s",
                    resp.status_code, attempt, _REGISTER_MAX_RETRIES,
                    resp.text[:200],
                )
            except asyncio.TimeoutError:
                log.warning(
                    "Paperclip registration timed out after %ds (attempt %d/%d)",
                    self.startup_timeout, attempt, _REGISTER_MAX_RETRIES,
                )
            except httpx.ConnectError:
                log.warning(
                    "Cannot reach Paperclip at %s (attempt %d/%d)",
                    self.url, attempt, _REGISTER_MAX_RETRIES,
                )
            except Exception:
                log.exception(
                    "Paperclip registration failed (attempt %d/%d)",
                    attempt, _REGISTER_MAX_RETRIES,
                )

            if attempt < _REGISTER_MAX_RETRIES:
                wait = _REGISTER_BACKOFF_BASE ** attempt
                log.info("Retrying Paperclip registration in %ds...", wait)
                await asyncio.sleep(wait)

        log.warning("Paperclip registration failed after %d attempts — will poll anyway", _REGISTER_MAX_RETRIES)

    async def stop(self) -> None:
        """Stop polling and close the client."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        log.info("Paperclip integration stopped")

    async def send_response(self, channel_id: str, content: str, **kwargs: Any) -> None:
        """Send a task result back to Paperclip with cost data."""
        if not self._client:
            return

        task_id = kwargs.get("task_id", channel_id)
        agent_name = kwargs.get("agent_name", "claude-daemon")

        # Build completion payload with cost tracking
        payload: dict[str, Any] = {
            "result": content,
            "agent": agent_name,
        }

        # Include cost data if available (from ClaudeResponse metadata)
        cost = kwargs.get("cost")
        if cost is not None:
            payload["cost_usd"] = cost
        input_tokens = kwargs.get("input_tokens")
        if input_tokens is not None:
            payload["input_tokens"] = input_tokens
        output_tokens = kwargs.get("output_tokens")
        if output_tokens is not None:
            payload["output_tokens"] = output_tokens

        try:
            resp = await self._client.post(
                f"/api/tasks/{task_id}/complete", json=payload,
            )
            if resp.status_code not in (200, 201, 202):
                log.warning(
                    "Paperclip task %s completion returned %d: %s",
                    task_id, resp.status_code, resp.text[:200],
                )
        except Exception:
            log.exception("Failed to send result to Paperclip for task %s", task_id)

    async def handle_heartbeat(self, payload: dict) -> dict:
        """Handle an incoming Paperclip heartbeat (webhook push).

        Called by the HTTP API when Paperclip POSTs to /api/paperclip/heartbeat.
        Returns the result synchronously (Paperclip waits for the response).
        """
        if not self._handler:
            return {"error": "No message handler configured", "status": "error"}

        # Extract task from heartbeat payload
        task = payload.get("task") or payload
        task_id = task.get("id") or task.get("task_id", "heartbeat")
        prompt = task.get("prompt") or task.get("description", "")
        agent_name = task.get("agent") or task.get("assigned_to")

        if not prompt:
            return {"error": "No prompt in heartbeat payload", "status": "error"}

        msg = NormalizedMessage(
            platform="paperclip",
            user_id=task.get("created_by", "paperclip"),
            user_name="Paperclip",
            content=prompt,
            message_id=str(task_id),
            channel_id=str(task_id),
            metadata={"task": task, "heartbeat": True},
        )

        # Route to specific agent if specified in the task
        if agent_name:
            msg.content = f"@{agent_name} {msg.content}"

        try:
            await self._handler(msg)
            return {"status": "ok", "task_id": task_id}
        except Exception:
            log.exception("Error processing Paperclip heartbeat task %s", task_id)
            return {"error": "Processing failed", "status": "error", "task_id": task_id}

    async def _poll_loop(self) -> None:
        """Continuously poll for pending tasks."""
        while self._running:
            try:
                await self._check_tasks()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Error polling Paperclip")

            await asyncio.sleep(self.poll_interval)

    async def _check_tasks(self) -> None:
        """Check for and process pending tasks."""
        if not self._client or not self._handler:
            return

        try:
            resp = await self._client.get("/api/tasks/pending", params={
                "agent": "claude-daemon",
                "limit": self.task_limit,
            })

            if resp.status_code != 200:
                if resp.status_code != 204:  # 204 = no content, expected
                    log.debug(
                        "Paperclip tasks endpoint returned %d: %s",
                        resp.status_code, resp.text[:200],
                    )
                return

            tasks = resp.json()
            if not isinstance(tasks, list):
                tasks = tasks.get("tasks", [])

            for task in tasks:
                task_id = task.get("id", "unknown")
                prompt = task.get("prompt") or task.get("description", "")
                agent_name = task.get("agent") or task.get("assigned_to")

                if not prompt:
                    continue

                content = prompt
                # Route to specific agent if specified in the task
                if agent_name:
                    content = f"@{agent_name} {prompt}"

                msg = NormalizedMessage(
                    platform="paperclip",
                    user_id=task.get("created_by", "paperclip"),
                    user_name="Paperclip",
                    content=content,
                    message_id=str(task_id),
                    channel_id=str(task_id),
                    metadata={"task": task},
                )

                try:
                    await self._handler(msg)
                except Exception:
                    log.exception("Error processing Paperclip task %s", task_id)

        except httpx.ConnectError:
            log.debug("Cannot reach Paperclip at %s", self.url)
        except Exception:
            log.exception("Error checking Paperclip tasks")
