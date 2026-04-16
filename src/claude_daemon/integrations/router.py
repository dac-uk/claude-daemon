"""MessageRouter - routes messages between integrations and the daemon core.

Includes rate limiting, streaming dispatch, and error alerting.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from claude_daemon.integrations.base import BaseIntegration, NormalizedMessage

if TYPE_CHECKING:
    from claude_daemon.core.daemon import ClaudeDaemon

log = logging.getLogger(__name__)

MAX_LENGTHS = {
    "telegram": 4096,
    "discord": 2000,
    "paperclip": 10000,
    "cli": 100000,
}


class RateLimiter:
    """Simple per-user rate limiter using a sliding window."""

    def __init__(self, max_requests: int = 20, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._timestamps: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, user_key: str) -> bool:
        now = time.monotonic()
        timestamps = self._timestamps[user_key]
        # Prune old entries
        self._timestamps[user_key] = [t for t in timestamps if now - t < self.window]
        if len(self._timestamps[user_key]) >= self.max_requests:
            return False
        self._timestamps[user_key].append(now)
        return True


class MessageRouter:
    """Routes messages between messaging platforms and the daemon."""

    def __init__(self, daemon: ClaudeDaemon) -> None:
        self.daemon = daemon
        self.integrations: dict[str, BaseIntegration] = {}
        self._rate_limiter = RateLimiter(
            max_requests=daemon.config.rate_limit_per_user,
            window_seconds=daemon.config.rate_limit_window,
        )

    def register(self, name: str, integration: BaseIntegration) -> None:
        self.integrations[name] = integration
        log.info("Registered integration: %s", name)

    async def handle_incoming(self, message: NormalizedMessage) -> str:
        """Handle an incoming message with rate limiting and error alerting."""
        log.info(
            "Incoming [%s] from %s (%s): %s",
            message.platform, message.user_name, message.user_id,
            message.content[:100],
        )

        # Rate limiting
        user_key = f"{message.platform}:{message.user_id}"
        if not self._rate_limiter.is_allowed(user_key):
            integration = self.integrations.get(message.platform)
            if integration:
                channel = message.channel_id or message.user_id
                await integration.send_response(
                    channel, "Rate limited. Please wait a moment before sending more messages."
                )
            return "rate_limited"

        try:
            # Get response from Claude (streaming is handled by the integration directly)
            response = await self.daemon.handle_message(
                prompt=message.content,
                platform=message.platform,
                user_id=message.user_id,
            )

            # Send response back via the originating integration
            integration = self.integrations.get(message.platform)
            if integration:
                channel = message.channel_id or message.user_id
                # Pass message metadata through for integrations that use it
                # (e.g. Paperclip uses task_id for completion reporting)
                send_kwargs: dict = {}
                if message.metadata:
                    send_kwargs["task_id"] = message.message_id
                    send_kwargs["metadata"] = message.metadata
                chunks = self._split_message(response, message.platform)
                for chunk in chunks:
                    await integration.send_response(channel, chunk, **send_kwargs)

            return response

        except Exception as e:
            log.exception("Error handling message from %s", user_key)
            # Alert the user about the error
            integration = self.integrations.get(message.platform)
            if integration:
                channel = message.channel_id or message.user_id
                await integration.send_response(
                    channel, f"An error occurred: {str(e)[:200]}"
                )
            return f"error: {e}"

    def _split_message(self, content: str, platform: str) -> list[str]:
        """Split a message into chunks respecting platform limits."""
        max_len = MAX_LENGTHS.get(platform, 4096)

        if len(content) <= max_len:
            return [content]

        chunks = []
        while content:
            if len(content) <= max_len:
                chunks.append(content)
                break

            split_at = content.rfind("\n", 0, max_len)
            if split_at < max_len // 2:
                split_at = content.rfind(" ", 0, max_len)
            if split_at < max_len // 2:
                split_at = max_len

            chunks.append(content[:split_at])
            content = content[split_at:].lstrip()

        return chunks
