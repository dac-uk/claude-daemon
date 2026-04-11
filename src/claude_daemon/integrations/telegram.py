"""Telegram bot integration with streaming responses and real commands."""

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
    from telegram import Update
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler as TGMessageHandler,
        filters,
    )

    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False

# Minimum interval between message edits (seconds) to avoid rate limiting
STREAM_EDIT_INTERVAL = 1.5


class TelegramIntegration(BaseIntegration):
    """Telegram bot with streaming responses and real working commands."""

    def __init__(
        self,
        token: str,
        allowed_users: list[int] | None = None,
        polling: bool = True,
        daemon: ClaudeDaemon | None = None,
    ) -> None:
        super().__init__()

        if not HAS_TELEGRAM:
            raise ImportError(
                "python-telegram-bot is required. Install with: "
                "pip install claude-daemon[telegram]"
            )

        self.token = token
        self.allowed_users = set(allowed_users) if allowed_users else set()
        self.polling = polling
        self.daemon = daemon
        self._app: Application | None = None

    async def start(self) -> None:
        builder = Application.builder().token(self.token)
        self._app = builder.build()

        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("agents", self._cmd_agents))
        self._app.add_handler(CommandHandler("newagent", self._cmd_newagent))
        self._app.add_handler(CommandHandler("setagent", self._cmd_setagent))
        self._app.add_handler(CommandHandler("delagent", self._cmd_delagent))
        self._app.add_handler(CommandHandler("memory", self._cmd_memory))
        self._app.add_handler(CommandHandler("forget", self._cmd_forget))
        self._app.add_handler(CommandHandler("session", self._cmd_session))
        self._app.add_handler(CommandHandler("cost", self._cmd_cost))
        self._app.add_handler(CommandHandler("jobs", self._cmd_jobs))
        self._app.add_handler(CommandHandler("dream", self._cmd_dream))
        self._app.add_handler(CommandHandler("soul", self._cmd_soul))
        self._app.add_handler(
            TGMessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started (polling mode)")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            log.info("Telegram bot stopped")

    async def send_response(self, channel_id: str, content: str, **kwargs: Any) -> None:
        if not self._app:
            return
        try:
            await self._app.bot.send_message(
                chat_id=int(channel_id), text=content, parse_mode=None,
            )
        except Exception:
            log.exception("Failed to send Telegram message to %s", channel_id)

    def _is_allowed(self, user_id: int) -> bool:
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages with streaming response."""
        if not update.message or not update.message.text:
            return

        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            await update.message.reply_text("You are not authorized to use this bot.")
            return

        if not self.daemon:
            if self._handler:
                msg = NormalizedMessage(
                    platform="telegram", user_id=str(user.id),
                    user_name=user.first_name or str(user.id),
                    content=update.message.text,
                    message_id=str(update.message.message_id),
                    channel_id=str(update.effective_chat.id) if update.effective_chat else None,
                )
                await self._handler(msg)
            return

        # Streaming mode: send placeholder, then edit with incoming chunks
        chat_id = update.effective_chat.id if update.effective_chat else user.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        # Send initial placeholder
        placeholder = await update.message.reply_text("...")

        accumulated = ""
        last_edit = time.monotonic()
        dirty = False

        try:
            async for chunk in self.daemon.handle_message_streaming(
                prompt=update.message.text,
                platform="telegram",
                user_id=str(user.id),
            ):
                if isinstance(chunk, str):
                    accumulated += chunk
                    dirty = True

                    # Throttled editing to respect Telegram rate limits
                    now = time.monotonic()
                    if now - last_edit >= STREAM_EDIT_INTERVAL and dirty:
                        display = accumulated if len(accumulated) <= 4096 else accumulated[-4096:]
                        try:
                            await placeholder.edit_text(display)
                            dirty = False
                            last_edit = now
                        except Exception:
                            pass  # Edit can fail if content unchanged

                elif isinstance(chunk, ClaudeResponse):
                    # Final result - do one last edit with complete text
                    final = accumulated or chunk.result
                    if final:
                        display = final if len(final) <= 4096 else final[-4096:]
                        try:
                            await placeholder.edit_text(display)
                        except Exception:
                            pass

                        # Send overflow as separate messages
                        if len(final) > 4096:
                            rest = final[:-4096]
                            while rest:
                                part = rest[:4096]
                                rest = rest[4096:]
                                await self._app.bot.send_message(
                                    chat_id=chat_id, text=part, parse_mode=None,
                                )

            # If we never got content, update placeholder
            if not accumulated:
                await placeholder.edit_text("(No response received)")

        except Exception:
            log.exception("Error in streaming response")
            try:
                await placeholder.edit_text("Sorry, an error occurred.")
            except Exception:
                pass

    # -- Working command handlers --

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        await update.message.reply_text(
            "Claude Daemon is running.\n\n"
            "Send any message to talk to the active agent.\n"
            "Use @agent_name to address a specific agent.\n\n"
            "Commands:\n"
            "/agents - List all agents\n"
            "/status - Daemon status and stats\n"
            "/memory - View persistent memory\n"
            "/soul - View agent identity\n"
            "/forget - Clear session, start fresh\n"
            "/session - Current session info\n"
            "/cost - Your usage costs\n"
            "/jobs - List scheduled jobs\n"
            "/dream - Trigger memory consolidation"
        )

    async def _cmd_agents(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon or not self.daemon.agent_registry:
            await update.message.reply_text("No agents loaded.")
            return

        lines = ["Agents:\n"]
        for agent in self.daemon.agent_registry:
            orch = " [orchestrator]" if agent.is_orchestrator else ""
            role = f" - {agent.identity.role}" if agent.identity.role else ""
            emoji = f"{agent.identity.emoji} " if agent.identity.emoji else ""
            model = f" [{agent.identity.default_model}]"
            lines.append(f"  {emoji}{agent.name}{role}{model}{orch}")
        lines.append(
            f"\nUse @agent_name to talk to a specific agent.\n"
            f"/newagent name role emoji - create agent\n"
            f"/setagent name field value - modify agent\n"
            f"/delagent name - remove agent"
        )
        await update.message.reply_text("\n".join(lines))

    async def _cmd_newagent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Create a new agent: /newagent name role emoji"""
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon:
            return

        args = (update.message.text or "").split(maxsplit=3)
        if len(args) < 3:
            await update.message.reply_text(
                "Usage: /newagent <name> <role> [emoji]\n"
                "Example: /newagent analyst 'Data Analyst' 📊"
            )
            return

        name = args[1]
        role = args[2]
        emoji = args[3] if len(args) > 3 else ""
        result = self.daemon.create_agent(name, role=role, emoji=emoji)
        await update.message.reply_text(result)

    async def _cmd_setagent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Modify an agent: /setagent name field value"""
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon:
            return

        args = (update.message.text or "").split(maxsplit=3)
        if len(args) < 4:
            await update.message.reply_text(
                "Usage: /setagent <name> <field> <value>\n"
                "Fields: role, emoji, model, soul, rules\n"
                "Example: /setagent penny model opus"
            )
            return

        result = self.daemon.update_agent(args[1], args[2], args[3])
        await update.message.reply_text(result)

    async def _cmd_delagent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Remove an agent: /delagent name"""
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon:
            return

        args = (update.message.text or "").split()
        if len(args) < 2:
            await update.message.reply_text("Usage: /delagent <name>")
            return

        result = self.daemon.delete_agent(args[1])
        await update.message.reply_text(result)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon or not self.daemon.store or not self.daemon.process_manager:
            await update.message.reply_text("Daemon not fully initialized.")
            return

        stats = self.daemon.store.get_stats()
        active = self.daemon.process_manager.active_count
        text = (
            f"Claude Daemon: running\n"
            f"Active processes: {active}\n"
            f"Total sessions: {stats.get('total', 0)}\n"
            f"Active sessions: {stats.get('active', 0)}\n"
            f"Total messages: {stats.get('total_messages', 0)}\n"
            f"Total cost: ${stats.get('total_cost', 0):.4f}\n"
            f"Streaming: {'enabled' if self.daemon.config.streaming_enabled else 'disabled'}"
        )
        await update.message.reply_text(text)

    async def _cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon or not self.daemon.durable:
            return

        memory = self.daemon.durable.read_memory()
        if memory:
            # Split if too long
            for i in range(0, len(memory), 4096):
                await update.message.reply_text(memory[i:i + 4096])
        else:
            await update.message.reply_text("No persistent memory yet. It builds over time.")

    async def _cmd_soul(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon or not self.daemon.durable:
            return

        soul = self.daemon.durable.read_soul()
        if soul:
            for i in range(0, len(soul), 4096):
                await update.message.reply_text(soul[i:i + 4096])
        else:
            await update.message.reply_text("No SOUL.md found.")

    async def _cmd_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon or not self.daemon.store:
            return

        self.daemon.store.reset_conversation(str(user.id), "telegram")
        await update.message.reply_text("Session cleared. Starting fresh!")

    async def _cmd_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon or not self.daemon.store:
            return

        conv = self.daemon.store.get_or_create_conversation(
            None, "telegram", str(user.id),
        )
        text = (
            f"Session ID: {conv['session_id'][:12]}...\n"
            f"Messages: {conv['message_count']}\n"
            f"Cost: ${conv['total_cost_usd']:.4f}\n"
            f"Started: {conv['started_at']}\n"
            f"Last active: {conv['last_active']}\n"
            f"Status: {conv['status']}"
        )
        await update.message.reply_text(text)

    async def _cmd_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon or not self.daemon.store:
            return

        stats = self.daemon.store.get_user_stats(str(user.id), "telegram")
        text = (
            f"Your usage (Telegram):\n"
            f"Sessions: {stats.get('sessions', 0)}\n"
            f"Messages: {stats.get('total_messages', 0)}\n"
            f"Total cost: ${stats.get('total_cost', 0):.4f}"
        )
        await update.message.reply_text(text)

    async def _cmd_jobs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon or not self.daemon.scheduler:
            return

        jobs = self.daemon.scheduler.list_jobs()
        lines = ["Scheduled jobs:\n"]
        for job in jobs:
            lines.append(f"  {job['id']:20s} next: {job['next_run']}")
        await update.message.reply_text("\n".join(lines))

    async def _cmd_dream(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        if not self.daemon or not self.daemon.compactor:
            return

        await update.message.reply_text("Triggering deep sleep consolidation...")
        try:
            await self.daemon.compactor.deep_sleep()
            await update.message.reply_text("Deep sleep complete. Memory consolidated.")
        except Exception as e:
            await update.message.reply_text(f"Dream failed: {str(e)[:200]}")
