"""AgentFileWatcher - polls agent workspace files for changes and triggers identity reload.

Uses os.stat() polling (no external dependencies) to detect mtime changes on key
agent config files (IDENTITY.md, SOUL.md, AGENTS.md, HEARTBEAT.md). When a change
is detected, the agent's identity is reloaded automatically.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.agents.registry import AgentRegistry

log = logging.getLogger(__name__)

# Files whose mtime changes trigger an identity reload
_WATCHED_FILES = ("IDENTITY.md", "SOUL.md", "AGENTS.md", "HEARTBEAT.md")


class AgentFileWatcher:
    """Polls agent workspace files for changes and triggers identity reload."""

    def __init__(self, registry: AgentRegistry, interval: int = 10) -> None:
        self.registry = registry
        self.interval = interval
        self._mtimes: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the background polling loop."""
        self._snapshot_all()  # Capture initial state
        self._task = asyncio.create_task(self._poll_loop())
        log.debug("Agent file watcher started (interval=%ds)", self.interval)

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task and not self._task.done():
            self._task.cancel()
            log.debug("Agent file watcher stopped")

    def _snapshot_all(self) -> None:
        """Capture initial mtime snapshot for all agents."""
        for agent in self.registry:
            for filename in _WATCHED_FILES:
                path = agent.workspace / filename
                key = f"{agent.name}:{filename}"
                try:
                    self._mtimes[key] = os.stat(path).st_mtime
                except OSError:
                    self._mtimes[key] = 0.0

        # Shared USER.md — changes here affect all agents
        shared_dir = getattr(self.registry, "shared_dir", None)
        if shared_dir:
            path = shared_dir / "USER.md"
            try:
                self._mtimes["__shared__:USER.md"] = os.stat(path).st_mtime
            except OSError:
                self._mtimes["__shared__:USER.md"] = 0.0

    async def _poll_loop(self) -> None:
        """Infinite polling loop that checks for file changes."""
        while True:
            try:
                await asyncio.sleep(self.interval)
                self._check_all()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Agent file watcher error")

    def _check_all(self) -> None:
        """Check all agents for file changes. Reload identity if any changed."""
        for agent in self.registry:
            changed = False
            for filename in _WATCHED_FILES:
                path = agent.workspace / filename
                key = f"{agent.name}:{filename}"
                try:
                    current_mtime = os.stat(path).st_mtime
                except OSError:
                    current_mtime = 0.0

                prev_mtime = self._mtimes.get(key, 0.0)
                if current_mtime != prev_mtime:
                    self._mtimes[key] = current_mtime
                    changed = True

            if changed:
                log.info("Hot-reload: %s config files changed, reloading identity", agent.name)
                try:
                    agent.load_identity()
                except Exception:
                    log.exception("Failed to reload identity for %s", agent.name)

        # Check shared USER.md — reload ALL agents if it changed
        shared_dir = getattr(self.registry, "shared_dir", None)
        if shared_dir:
            path = shared_dir / "USER.md"
            key = "__shared__:USER.md"
            try:
                current_mtime = os.stat(path).st_mtime
            except OSError:
                current_mtime = 0.0
            if current_mtime != self._mtimes.get(key, 0.0):
                self._mtimes[key] = current_mtime
                log.info("Hot-reload: shared USER.md changed, reloading all agents")
                for agent in self.registry:
                    try:
                        agent.load_identity()
                    except Exception:
                        log.exception("Failed to reload identity for %s", agent.name)
