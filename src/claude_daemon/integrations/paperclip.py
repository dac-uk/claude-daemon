"""Paperclip integration - REST API polling for multi-agent orchestration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from claude_daemon.integrations.base import BaseIntegration, NormalizedMessage

log = logging.getLogger(__name__)


class PaperclipIntegration(BaseIntegration):
    """Connects to Paperclip's REST API as an agent.

    Polls for pending tasks and sends results back.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        poll_interval: int = 5,
    ) -> None:
        super().__init__()
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.poll_interval = poll_interval
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._running = False

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

        # Register as agent
        try:
            resp = await self._client.post("/api/agents/register", json={
                "name": "claude-daemon",
                "type": "claude-code",
                "capabilities": ["code", "analysis", "general"],
            })
            if resp.status_code in (200, 201):
                log.info("Registered with Paperclip at %s", self.url)
            else:
                log.warning("Paperclip registration response: %d", resp.status_code)
        except Exception:
            log.warning("Could not register with Paperclip (will poll anyway)")

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        log.info("Paperclip integration started (polling every %ds)", self.poll_interval)

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
        """Send a task result back to Paperclip."""
        if not self._client:
            return

        task_id = kwargs.get("task_id", channel_id)
        try:
            await self._client.post(f"/api/tasks/{task_id}/complete", json={
                "result": content,
                "agent": "claude-daemon",
            })
        except Exception:
            log.exception("Failed to send result to Paperclip for task %s", task_id)

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
                "limit": 5,
            })

            if resp.status_code != 200:
                return

            tasks = resp.json()
            if not isinstance(tasks, list):
                tasks = tasks.get("tasks", [])

            for task in tasks:
                task_id = task.get("id", "unknown")
                prompt = task.get("prompt") or task.get("description", "")

                if not prompt:
                    continue

                msg = NormalizedMessage(
                    platform="paperclip",
                    user_id=task.get("created_by", "paperclip"),
                    user_name="Paperclip",
                    content=prompt,
                    message_id=task_id,
                    channel_id=task_id,
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
