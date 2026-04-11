"""Discord bot integration using discord.py."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from claude_daemon.integrations.base import BaseIntegration, NormalizedMessage

log = logging.getLogger(__name__)

try:
    import discord
    from discord.ext import commands

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


class DiscordIntegration(BaseIntegration):
    """Discord bot with slash commands and DM support."""

    def __init__(
        self,
        token: str,
        allowed_guilds: list[int] | None = None,
    ) -> None:
        super().__init__()

        if not HAS_DISCORD:
            raise ImportError(
                "discord.py is required. Install with: "
                "pip install claude-daemon[discord]"
            )

        self.token = token
        self.allowed_guilds = set(allowed_guilds) if allowed_guilds else set()

        intents = discord.Intents.default()
        intents.message_content = True
        self._bot = commands.Bot(command_prefix="!", intents=intents)
        self._task: asyncio.Task | None = None

        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register event handlers and commands."""

        @self._bot.event
        async def on_ready():
            log.info("Discord bot connected as %s", self._bot.user)
            try:
                synced = await self._bot.tree.sync()
                log.info("Synced %d slash commands", len(synced))
            except Exception:
                log.exception("Failed to sync slash commands")

        @self._bot.event
        async def on_message(message: discord.Message):
            if message.author == self._bot.user:
                return

            # Only respond in DMs or when mentioned
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = self._bot.user in message.mentions if self._bot.user else False

            if not is_dm and not is_mentioned:
                return

            # Check guild allowlist
            if message.guild and self.allowed_guilds:
                if message.guild.id not in self.allowed_guilds:
                    return

            if not self._handler:
                await message.reply("Bot is not fully initialized.")
                return

            # Clean the mention from content
            content = message.content
            if self._bot.user:
                content = content.replace(f"<@{self._bot.user.id}>", "").strip()

            if not content:
                return

            msg = NormalizedMessage(
                platform="discord",
                user_id=str(message.author.id),
                user_name=message.author.display_name,
                content=content,
                message_id=str(message.id),
                channel_id=str(message.channel.id),
            )

            # Show typing indicator
            async with message.channel.typing():
                try:
                    await self._handler(msg)
                except Exception:
                    log.exception("Error handling Discord message")
                    await message.reply("Sorry, an error occurred processing your message.")

        # Slash commands
        @self._bot.tree.command(name="status", description="Show Claude Daemon status")
        async def slash_status(interaction: discord.Interaction):
            await interaction.response.send_message("Claude Daemon: running")

        @self._bot.tree.command(name="memory", description="View persistent memory")
        async def slash_memory(interaction: discord.Interaction):
            await interaction.response.defer()
            if self._handler:
                msg = NormalizedMessage(
                    platform="discord",
                    user_id=str(interaction.user.id),
                    user_name=interaction.user.display_name,
                    content="Show me your current persistent memory.",
                    message_id="slash",
                    channel_id=str(interaction.channel_id),
                )
                await self._handler(msg)
            else:
                await interaction.followup.send("Bot not initialized.")

        @self._bot.tree.command(name="forget", description="Start a fresh conversation")
        async def slash_forget(interaction: discord.Interaction):
            await interaction.response.send_message("Session cleared. Starting fresh!")

    async def start(self) -> None:
        """Start the Discord bot in a background task."""
        self._task = asyncio.create_task(self._bot.start(self.token))
        # Give it a moment to connect
        await asyncio.sleep(2)
        log.info("Discord bot starting...")

    async def stop(self) -> None:
        """Stop the Discord bot."""
        await self._bot.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Discord bot stopped")

    async def send_response(self, channel_id: str, content: str, **kwargs: Any) -> None:
        """Send a message to a Discord channel."""
        try:
            channel = self._bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self._bot.fetch_channel(int(channel_id))

            if channel and hasattr(channel, "send"):
                # Split long messages
                if len(content) > 2000:
                    chunks = []
                    while content:
                        if len(content) <= 2000:
                            chunks.append(content)
                            break
                        split = content.rfind("\n", 0, 2000)
                        if split < 1000:
                            split = 2000
                        chunks.append(content[:split])
                        content = content[split:].lstrip()

                    for chunk in chunks:
                        await channel.send(chunk)
                else:
                    await channel.send(content)
        except Exception:
            log.exception("Failed to send Discord message to %s", channel_id)
