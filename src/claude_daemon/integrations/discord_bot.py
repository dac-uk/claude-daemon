"""Discord bot integration with streaming and slash commands."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from claude_daemon.integrations.base import BaseIntegration, NormalizedMessage
from claude_daemon.core.process import ClaudeResponse

if TYPE_CHECKING:
    from claude_daemon.core.daemon import ClaudeDaemon

log = logging.getLogger(__name__)

try:
    import discord
    from discord.ext import commands

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

STREAM_EDIT_INTERVAL = 1.0  # Discord rate limit is stricter


class DiscordIntegration(BaseIntegration):
    """Discord bot with streaming responses and slash commands."""

    def __init__(
        self,
        token: str,
        allowed_guilds: list[int] | None = None,
        daemon: ClaudeDaemon | None = None,
    ) -> None:
        super().__init__()

        if not HAS_DISCORD:
            raise ImportError(
                "discord.py is required. Install with: "
                "pip install claude-daemon[discord]"
            )

        self.token = token
        self.allowed_guilds = set(allowed_guilds) if allowed_guilds else set()
        self.daemon = daemon

        intents = discord.Intents.default()
        intents.message_content = True
        self._bot = commands.Bot(command_prefix="!", intents=intents)
        self._task: asyncio.Task | None = None

        self._register_handlers()

    def _register_handlers(self) -> None:
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

            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = self._bot.user in message.mentions if self._bot.user else False

            if not is_dm and not is_mentioned:
                return

            if message.guild and self.allowed_guilds:
                if message.guild.id not in self.allowed_guilds:
                    return

            content = message.content
            if self._bot.user:
                content = content.replace(f"<@{self._bot.user.id}>", "").strip()
            if not content:
                return

            # Streaming response via message editing
            if self.daemon:
                await self._handle_streaming(message, content)
            elif self._handler:
                msg = NormalizedMessage(
                    platform="discord", user_id=str(message.author.id),
                    user_name=message.author.display_name,
                    content=content, message_id=str(message.id),
                    channel_id=str(message.channel.id),
                )
                async with message.channel.typing():
                    await self._handler(msg)

        # Slash commands
        @self._bot.tree.command(name="status", description="Show daemon status")
        async def slash_status(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.store:
                await interaction.response.send_message("Not initialized.")
                return
            stats = self.daemon.store.get_stats()
            active = self.daemon.process_manager.active_count if self.daemon.process_manager else 0
            await interaction.response.send_message(
                f"**Claude Daemon**\n"
                f"Active: {active} | Sessions: {stats.get('total', 0)} | "
                f"Messages: {stats.get('total_messages', 0)} | "
                f"Cost: ${stats.get('total_cost', 0):.4f}"
            )

        @self._bot.tree.command(name="memory", description="View persistent memory")
        async def slash_memory(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.durable:
                await interaction.response.send_message("Not initialized.")
                return
            memory = self.daemon.durable.read_memory()
            text = memory[:2000] if memory else "No persistent memory yet."
            await interaction.response.send_message(text)

        @self._bot.tree.command(name="forget", description="Start a fresh conversation")
        async def slash_forget(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.store:
                await interaction.response.send_message("Not initialized.")
                return
            self.daemon.store.reset_conversation(str(interaction.user.id), "discord")
            await interaction.response.send_message("Session cleared. Starting fresh!")

        @self._bot.tree.command(name="soul", description="View agent identity")
        async def slash_soul(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.durable:
                await interaction.response.send_message("Not initialized.")
                return
            soul = self.daemon.durable.read_soul()
            await interaction.response.send_message(soul[:2000] if soul else "No SOUL.md.")

        @self._bot.tree.command(name="cost", description="Your usage costs")
        async def slash_cost(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.store:
                await interaction.response.send_message("Not initialized.")
                return
            stats = self.daemon.store.get_user_stats(str(interaction.user.id), "discord")
            await interaction.response.send_message(
                f"Sessions: {stats.get('sessions', 0)} | "
                f"Messages: {stats.get('total_messages', 0)} | "
                f"Cost: ${stats.get('total_cost', 0):.4f}"
            )

        @self._bot.tree.command(name="dream", description="Trigger memory consolidation")
        async def slash_dream(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.compactor:
                await interaction.response.send_message("Not initialized.")
                return
            await interaction.response.defer()
            try:
                await self.daemon.compactor.deep_sleep()
                await interaction.followup.send("Deep sleep complete. Memory consolidated.")
            except Exception as e:
                await interaction.followup.send(f"Dream failed: {str(e)[:200]}")

    async def _handle_streaming(self, message: discord.Message, content: str) -> None:
        """Stream response by editing a message progressively."""
        placeholder = await message.reply("...")

        accumulated = ""
        last_edit = time.monotonic()

        try:
            async for chunk in self.daemon.handle_message_streaming(
                prompt=content,
                platform="discord",
                user_id=str(message.author.id),
            ):
                if isinstance(chunk, str):
                    accumulated += chunk

                    now = time.monotonic()
                    if now - last_edit >= STREAM_EDIT_INTERVAL:
                        display = accumulated[-2000:] if len(accumulated) > 2000 else accumulated
                        try:
                            await placeholder.edit(content=display)
                            last_edit = now
                        except Exception:
                            pass

                elif isinstance(chunk, ClaudeResponse):
                    final = accumulated or chunk.result
                    if final:
                        if len(final) <= 2000:
                            try:
                                await placeholder.edit(content=final)
                            except Exception:
                                pass
                        else:
                            # Split into multiple messages
                            try:
                                await placeholder.edit(content=final[:2000])
                            except Exception:
                                pass
                            for i in range(2000, len(final), 2000):
                                await message.channel.send(final[i:i + 2000])

            if not accumulated:
                await placeholder.edit(content="(No response received)")

        except Exception:
            log.exception("Error in Discord streaming")
            try:
                await placeholder.edit(content="Sorry, an error occurred.")
            except Exception:
                pass

    async def start(self) -> None:
        self._task = asyncio.create_task(self._bot.start(self.token))
        await asyncio.sleep(2)

    async def stop(self) -> None:
        await self._bot.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def send_response(self, channel_id: str, content: str, **kwargs: Any) -> None:
        try:
            channel = self._bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self._bot.fetch_channel(int(channel_id))
            if channel and hasattr(channel, "send"):
                for i in range(0, len(content), 2000):
                    await channel.send(content[i:i + 2000])
        except Exception:
            log.exception("Failed to send Discord message to %s", channel_id)
