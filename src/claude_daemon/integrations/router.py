"""MessageRouter - routes messages between integrations and the daemon core."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from claude_daemon.integrations.base import BaseIntegration, NormalizedMessage

if TYPE_CHECKING:
    from claude_daemon.core.daemon import ClaudeDaemon

log = logging.getLogger(__name__)

# Platform-specific message length limits
MAX_LENGTHS = {
    "telegram": 4096,
    "discord": 2000,
    "paperclip": 10000,
    "cli": 100000,
}


class MessageRouter:
    """Routes messages between messaging platforms and the daemon."""

    def __init__(self, daemon: ClaudeDaemon) -> None:
        self.daemon = daemon
        self.integrations: dict[str, BaseIntegration] = {}

    def register(self, name: str, integration: BaseIntegration) -> None:
        """Register a messaging integration."""
        self.integrations[name] = integration
        log.info("Registered integration: %s", name)

    async def handle_incoming(self, message: NormalizedMessage) -> str:
        """Handle an incoming message from any platform.

        1. Route to daemon.handle_message
        2. Format response for the originating platform
        3. Send response back
        """
        log.info(
            "Incoming [%s] from %s (%s): %s",
            message.platform,
            message.user_name,
            message.user_id,
            message.content[:100],
        )

        # Get response from Claude
        response = await self.daemon.handle_message(
            prompt=message.content,
            platform=message.platform,
            user_id=message.user_id,
        )

        # Send response back via the originating integration
        integration = self.integrations.get(message.platform)
        if integration:
            channel = message.channel_id or message.user_id
            formatted = self._format_response(response, message.platform)

            # Split long messages
            chunks = self._split_message(formatted, message.platform)
            for chunk in chunks:
                await integration.send_response(channel, chunk, reply_to=message.message_id)

        return response

    def _format_response(self, content: str, platform: str) -> str:
        """Apply platform-specific formatting to response content."""
        if platform == "telegram":
            # Telegram uses MarkdownV2 - escape special chars if needed
            return content
        elif platform == "discord":
            # Discord supports standard markdown
            return content
        return content

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

            # Try to split at a newline
            split_at = content.rfind("\n", 0, max_len)
            if split_at < max_len // 2:
                # No good newline, split at space
                split_at = content.rfind(" ", 0, max_len)
            if split_at < max_len // 2:
                # No good space either, hard split
                split_at = max_len

            chunks.append(content[:split_at])
            content = content[split_at:].lstrip()

        return chunks
