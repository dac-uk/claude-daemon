"""Discord bot integration with streaming, full slash commands, and agent channels."""

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

STREAM_EDIT_INTERVAL = 1.0


class DiscordIntegration(BaseIntegration):
    """Discord bot with streaming responses and full slash command parity."""

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

            # Also respond in agent-bound channels without needing @mention
            channel_agent = None
            if self.daemon and self.daemon.config.discord_agent_channels:
                channel_agent = self.daemon.config.discord_agent_channels.get(
                    str(message.channel.id)
                )

            if not is_dm and not is_mentioned and not channel_agent:
                return

            if message.guild and self.allowed_guilds:
                if message.guild.id not in self.allowed_guilds:
                    return

            # Rate limiting
            if self.daemon and self.daemon.router:
                user_key = f"discord:{message.author.id}"
                if not self.daemon.router._rate_limiter.is_allowed(user_key):
                    await message.reply("Rate limited. Please wait before sending more messages.")
                    return

            content = message.content
            if self._bot.user:
                content = content.replace(f"<@{self._bot.user.id}>", "").strip()
            if not content:
                return

            if self.daemon:
                await self._handle_streaming(message, content, channel_agent)
            elif self._handler:
                msg = NormalizedMessage(
                    platform="discord", user_id=str(message.author.id),
                    user_name=message.author.display_name,
                    content=content, message_id=str(message.id),
                    channel_id=str(message.channel.id),
                )
                async with message.channel.typing():
                    await self._handler(msg)

        # -- Slash commands (full parity with Telegram) --

        @self._bot.tree.command(name="start", description="Show help and available commands")
        async def slash_start(interaction: discord.Interaction):
            await interaction.response.send_message(
                "**Claude Daemon**\n\n"
                "Send a message (mention me or DM) to talk to the active agent.\n"
                "Use `@agent_name` to address a specific agent.\n\n"
                "**Commands:**\n"
                "`/agents` - List all agents\n"
                "`/status` - Daemon status\n"
                "`/memory` - View persistent memory\n"
                "`/soul` - View agent identity\n"
                "`/forget` - Clear session\n"
                "`/session` - Current session info\n"
                "`/cost` - Your usage costs\n"
                "`/jobs` - List scheduled jobs\n"
                "`/dream` - Trigger memory consolidation\n"
                "`/workflow` - Run build quality gate\n"
                "`/metrics` - Per-agent cost metrics\n"
                "`/spawn` - Background task spawning\n"
                "`/tasks` - List active tasks\n"
                "`/newagent` - Create an agent\n"
                "`/setagent` - Modify an agent\n"
                "`/delagent` - Remove an agent\n"
                "`/setenv` - Set an env var\n"
                "`/getenv` - Show env var status"
            )

        @self._bot.tree.command(name="status", description="Show daemon status")
        async def slash_status(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.store:
                await interaction.response.send_message("Not initialized.")
                return
            stats = self.daemon.store.get_stats()
            active = self.daemon.process_manager.active_count if self.daemon.process_manager else 0
            await interaction.response.send_message(
                f"**Claude Daemon: running**\n"
                f"Active processes: {active}\n"
                f"Total sessions: {stats.get('total', 0)}\n"
                f"Active sessions: {stats.get('active', 0)}\n"
                f"Total messages: {stats.get('total_messages', 0)}\n"
                f"Total cost: ${stats.get('total_cost', 0):.4f}\n"
                f"Streaming: {'enabled' if self.daemon.config.streaming_enabled else 'disabled'}"
            )

        @self._bot.tree.command(name="agents", description="List all agents")
        async def slash_agents(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.agent_registry:
                await interaction.response.send_message("No agents loaded.")
                return
            lines = ["**Agents:**\n"]
            for agent in self.daemon.agent_registry:
                orch = " [orchestrator]" if agent.is_orchestrator else ""
                role = f" - {agent.identity.role}" if agent.identity.role else ""
                emoji = f"{agent.identity.emoji} " if agent.identity.emoji else ""
                model = f" [{agent.identity.default_model}]"
                lines.append(f"  {emoji}{agent.name}{role}{model}{orch}")
            lines.append(
                f"\nUse `@agent_name` to talk to a specific agent.\n"
                f"`/newagent` `/setagent` `/delagent` to manage agents."
            )
            await interaction.response.send_message("\n".join(lines))

        @self._bot.tree.command(name="newagent", description="Create a new agent")
        @discord.app_commands.describe(
            name="Agent name", role="Agent role", emoji="Agent emoji"
        )
        async def slash_newagent(
            interaction: discord.Interaction, name: str, role: str, emoji: str = "",
        ):
            if not self.daemon:
                await interaction.response.send_message("Not initialized.")
                return
            result = self.daemon.create_agent(name, role=role, emoji=emoji)
            await interaction.response.send_message(result)

        @self._bot.tree.command(name="setagent", description="Modify an agent")
        @discord.app_commands.describe(
            name="Agent name", field="Field (role, emoji, model, soul, rules)", value="New value"
        )
        async def slash_setagent(
            interaction: discord.Interaction, name: str, field: str, value: str,
        ):
            if not self.daemon:
                await interaction.response.send_message("Not initialized.")
                return
            result = self.daemon.update_agent(name, field, value)
            await interaction.response.send_message(result)

        @self._bot.tree.command(name="delagent", description="Remove an agent")
        @discord.app_commands.describe(name="Agent name")
        async def slash_delagent(interaction: discord.Interaction, name: str):
            if not self.daemon:
                await interaction.response.send_message("Not initialized.")
                return
            result = self.daemon.delete_agent(name)
            await interaction.response.send_message(result)

        @self._bot.tree.command(name="memory", description="View persistent memory")
        async def slash_memory(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.durable:
                await interaction.response.send_message("Not initialized.")
                return
            memory = self.daemon.durable.read_memory()
            text = memory[:2000] if memory else "No persistent memory yet."
            await interaction.response.send_message(text)

        @self._bot.tree.command(name="soul", description="View agent identity")
        async def slash_soul(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.durable:
                await interaction.response.send_message("Not initialized.")
                return
            soul = self.daemon.durable.read_soul()
            await interaction.response.send_message(soul[:2000] if soul else "No SOUL.md.")

        @self._bot.tree.command(name="forget", description="Start a fresh conversation")
        async def slash_forget(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.store:
                await interaction.response.send_message("Not initialized.")
                return
            self.daemon.store.reset_conversation(str(interaction.user.id))
            await interaction.response.send_message("Session cleared. Starting fresh!")

        @self._bot.tree.command(name="session", description="Current session info")
        async def slash_session(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.store:
                await interaction.response.send_message("Not initialized.")
                return
            conv = self.daemon.store.get_or_create_conversation(
                None, "discord", str(interaction.user.id),
            )
            await interaction.response.send_message(
                f"Session: {conv['session_id'][:12]}...\n"
                f"Messages: {conv['message_count']}\n"
                f"Cost: ${conv['total_cost_usd']:.4f}\n"
                f"Started: {conv['started_at']}\n"
                f"Last active: {conv['last_active']}"
            )

        @self._bot.tree.command(name="cost", description="Your usage costs")
        async def slash_cost(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.store:
                await interaction.response.send_message("Not initialized.")
                return
            stats = self.daemon.store.get_user_stats(str(interaction.user.id))
            await interaction.response.send_message(
                f"**Your usage:**\n"
                f"Sessions: {stats.get('sessions', 0)}\n"
                f"Messages: {stats.get('total_messages', 0)}\n"
                f"Cost: ${stats.get('total_cost', 0):.4f}"
            )

        @self._bot.tree.command(name="jobs", description="List scheduled jobs")
        async def slash_jobs(interaction: discord.Interaction):
            if not self.daemon or not self.daemon.scheduler:
                await interaction.response.send_message("Not initialized.")
                return
            jobs = self.daemon.scheduler.list_jobs()
            lines = ["**Scheduled jobs:**\n"]
            for job in jobs:
                lines.append(f"  `{job['id'][:25]}` next: {job['next_run']}")
            await interaction.response.send_message("\n".join(lines))

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

        @self._bot.tree.command(name="workflow", description="Run build quality gate workflow")
        @discord.app_commands.describe(request="What to build")
        async def slash_workflow(interaction: discord.Interaction, request: str):
            if not self.daemon:
                await interaction.response.send_message("Not initialized.")
                return
            await interaction.response.defer()
            try:
                result = await self.daemon.run_build_workflow(request)
                # Split if needed
                for i in range(0, len(result), 2000):
                    if i == 0:
                        await interaction.followup.send(result[i:i + 2000])
                    else:
                        await interaction.followup.send(result[i:i + 2000])
            except Exception as e:
                await interaction.followup.send(f"Workflow failed: {str(e)[:200]}")

        @self._bot.tree.command(name="metrics", description="Per-agent cost metrics")
        @discord.app_commands.describe(agent="Agent name (optional)")
        async def slash_metrics(interaction: discord.Interaction, agent: str = ""):
            if not self.daemon or not self.daemon.store:
                await interaction.response.send_message("Not initialized.")
                return
            metrics = self.daemon.store.get_agent_metrics(
                agent_name=agent if agent else None, days=7,
            )
            if not metrics:
                await interaction.response.send_message("No metrics in the last 7 days.")
                return
            lines = ["**Agent metrics (7 days):**\n"]
            for m in metrics:
                lines.append(
                    f"  {m.get('agent_name', '?'):12s} "
                    f"calls={m.get('count', 0)} "
                    f"cost=${m.get('total_cost', 0):.4f} "
                    f"tokens={m.get('total_input', 0) + m.get('total_output', 0)}"
                )
            await interaction.response.send_message("\n".join(lines))

        @self._bot.tree.command(name="spawn", description="Spawn a background task on an agent")
        @discord.app_commands.describe(agent="Agent name", task="Task description")
        async def slash_spawn(interaction: discord.Interaction, agent: str, task: str):
            if not self.daemon:
                await interaction.response.send_message("Not initialized.")
                return
            result = self.daemon.spawn_task(agent, task)
            await interaction.response.send_message(result)

        @self._bot.tree.command(name="tasks", description="List spawned background tasks")
        async def slash_tasks(interaction: discord.Interaction):
            if not self.daemon:
                await interaction.response.send_message("Not initialized.")
                return
            result = self.daemon.list_tasks()
            await interaction.response.send_message(result)

        @self._bot.tree.command(name="setenv", description="Set an environment variable")
        @discord.app_commands.describe(key="Variable name (e.g. GITHUB_TOKEN)", value="Value")
        async def slash_setenv(interaction: discord.Interaction, key: str, value: str):
            try:
                from claude_daemon.core.env_manager import set_env_var, reload_env
                key = key.upper()
                set_env_var(key, value)
                reload_env()
                if self.daemon:
                    await self.daemon.reload_config()
                masked = "****" + value[-4:] if len(value) >= 4 else "****"
                note = ""
                if key in ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN"):
                    note = "\nNote: integration tokens require a daemon restart to take effect."
                from claude_daemon.core.env_manager import detect_mcp_server_for_var
                mcp_server = detect_mcp_server_for_var(key)
                if mcp_server:
                    note += (
                        f"\nThis enables the `{mcp_server}` MCP server."
                        f"\nUse `/mcp refresh` to apply now."
                    )
                await interaction.response.send_message(
                    f"Set `{key}` = `{masked}`{note}", ephemeral=True,
                )
            except ValueError as e:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)

        @self._bot.tree.command(name="getenv", description="Show environment variable status")
        async def slash_getenv(interaction: discord.Interaction):
            from claude_daemon.core.env_manager import list_env_vars
            env_vars = list_env_vars()
            lines = ["**Environment variables:**\n"]
            for var in env_vars:
                if var["status"] == "set":
                    lines.append(f"`{var['key']}`: {var['masked']}")
                else:
                    lines.append(f"`{var['key']}`: *(not set)*")
            lines.append("\nSet with: `/setenv KEY value`")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @self._bot.tree.command(name="mcp", description="MCP server pool management")
        @discord.app_commands.describe(
            action="list, enable, disable, or refresh",
            server="Server name (for enable/disable)",
        )
        async def slash_mcp(
            interaction: discord.Interaction,
            action: str = "list",
            server: str = "",
        ):
            if not self.daemon:
                await interaction.response.send_message("Daemon not ready.", ephemeral=True)
                return

            if action == "list":
                statuses = self.daemon.get_mcp_status()
                lines = ["**MCP Server Pool**\n"]
                by_cat: dict[str, list] = {}
                for s in statuses:
                    by_cat.setdefault(s["category"], []).append(s)
                for cat, servers_list in sorted(by_cat.items()):
                    lines.append(f"**[{cat}]**")
                    for s in servers_list:
                        icon = {"active": ":white_check_mark:", "inactive": ":orange_circle:",
                                "disabled": ":no_entry:"}
                        mark = icon.get(s["status"], ":grey_question:")
                        extra = ""
                        if s["status"] == "inactive":
                            missing = [k for k, v in s["env_status"].items() if v == "unset"]
                            extra = f" *(needs: {', '.join(missing)})*"
                        lines.append(f"{mark} `{s['name']}` — {s['description']}{extra}")
                lines.append(
                    "\n`/mcp enable <name>` | `/mcp disable <name>` | `/mcp refresh`"
                )
                text = "\n".join(lines)
                # Discord 2000 char limit — truncate if needed
                if len(text) > 1990:
                    text = text[:1990] + "..."
                await interaction.response.send_message(text, ephemeral=True)

            elif action == "enable" and server:
                result = await self.daemon.enable_mcp_server(server)
                await interaction.response.send_message(result, ephemeral=True)

            elif action == "disable" and server:
                result = await self.daemon.disable_mcp_server(server)
                await interaction.response.send_message(result, ephemeral=True)

            elif action == "refresh":
                result = await self.daemon.refresh_mcp()
                await interaction.response.send_message(result, ephemeral=True)

            else:
                await interaction.response.send_message(
                    "Usage: `/mcp list` | `/mcp enable <server>` | "
                    "`/mcp disable <server>` | `/mcp refresh`",
                    ephemeral=True,
                )

        @self._bot.tree.command(name="thinking", description="Toggle extended thinking for all agents")
        @discord.app_commands.describe(toggle="on or off")
        async def slash_thinking(interaction: discord.Interaction, toggle: str = ""):
            if not self.daemon:
                await interaction.response.send_message("Daemon not ready.", ephemeral=True)
                return
            if toggle.lower() not in ("on", "off"):
                current = "on" if self.daemon.config.thinking_enabled else "off"
                await interaction.response.send_message(
                    f"Usage: `/thinking on|off`\n\n"
                    f"Toggles extended thinking for all agents.\n"
                    f"Currently: {current}",
                    ephemeral=True,
                )
                return
            enabled = toggle.lower() == "on"
            result = await self.daemon.set_thinking(enabled)
            await interaction.response.send_message(result, ephemeral=True)

        @self._bot.tree.command(name="effort", description="Set reasoning depth for all tasks")
        @discord.app_commands.describe(level="low, medium, high, or max")
        async def slash_effort(interaction: discord.Interaction, level: str = ""):
            if not self.daemon:
                await interaction.response.send_message("Daemon not ready.", ephemeral=True)
                return
            if not level:
                current = self.daemon.config.default_effort or "per-task-type"
                await interaction.response.send_message(
                    f"Usage: `/effort low|medium|high|max`\n\n"
                    f"Sets reasoning depth for all tasks.\n"
                    f"Currently: {current}",
                    ephemeral=True,
                )
                return
            result = await self.daemon.set_default_effort(level.lower())
            await interaction.response.send_message(result, ephemeral=True)

        @self._bot.tree.command(name="backend", description="Control Managed Agents backend")
        @discord.app_commands.describe(action="on, off, or status")
        async def slash_backend(interaction: discord.Interaction, action: str = ""):
            if not self.daemon:
                await interaction.response.send_message("Daemon not ready.", ephemeral=True)
                return
            if action.lower() in ("on", "enable"):
                result = await self.daemon.set_managed_agents(True)
                await interaction.response.send_message(result, ephemeral=True)
            elif action.lower() in ("off", "disable"):
                result = await self.daemon.set_managed_agents(False)
                await interaction.response.send_message(result, ephemeral=True)
            else:
                status = self.daemon.get_managed_agents_status()
                lines = [
                    "**Managed Agents Backend**",
                    f"Enabled: {status['enabled']}",
                    f"API key set: {status['api_key_set']}",
                    f"Environment: {status['environment_id'] or 'none'}",
                    f"Registered: {', '.join(status['registered_agents']) or 'none'}",
                    f"Task types: {', '.join(status['task_types'])}",
                ]
                await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _handle_streaming(
        self, message: discord.Message, content: str,
        channel_agent: str | None = None,
    ) -> None:
        """Stream response by editing a message progressively.

        If the response takes a long time and newer messages have appeared
        in the channel, the final result is posted as a new message at the
        bottom instead of editing the old placeholder — so the user doesn't
        have to scan backwards.
        """
        placeholder = await message.reply("...")
        placeholder_id = placeholder.id

        accumulated = ""
        last_edit = time.monotonic()
        start_time = time.monotonic()

        try:
            async for chunk in self.daemon.handle_message_streaming(
                prompt=content,
                platform="discord",
                user_id=str(message.author.id),
                agent_name=channel_agent,
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
                    if final and not accumulated:
                        accumulated = final
                    if final:
                        elapsed = time.monotonic() - start_time
                        is_stale = False
                        if elapsed > 10:
                            try:
                                recent = [m async for m in message.channel.history(limit=1)]
                                if recent and recent[0].id != placeholder_id:
                                    is_stale = True
                            except Exception:
                                pass

                        if is_stale:
                            try:
                                await placeholder.delete()
                            except Exception:
                                try:
                                    await placeholder.edit(content="(See response below)")
                                except Exception:
                                    pass
                            if len(final) <= 2000:
                                await message.channel.send(final)
                            else:
                                for i in range(0, len(final), 2000):
                                    await message.channel.send(final[i:i + 2000])
                        else:
                            # Delete placeholder and send as a new reply so Discord
                            # triggers a real notification (edits don't notify).
                            deleted = False
                            try:
                                await placeholder.delete()
                                deleted = True
                            except Exception:
                                pass

                            first_chunk = final[:2000]
                            try:
                                await message.reply(first_chunk)
                            except Exception:
                                if not deleted:
                                    try:
                                        await placeholder.edit(content=first_chunk)
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
