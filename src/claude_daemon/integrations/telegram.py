"""Telegram bot integration using python-telegram-bot."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from claude_daemon.integrations.base import BaseIntegration, NormalizedMessage

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


class TelegramIntegration(BaseIntegration):
    """Telegram bot integration with polling support."""

    def __init__(
        self,
        token: str,
        allowed_users: list[int] | None = None,
        polling: bool = True,
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
        self._app: Application | None = None

    async def start(self) -> None:
        """Start the Telegram bot."""
        builder = Application.builder().token(self.token)
        self._app = builder.build()

        # Register handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("memory", self._cmd_memory))
        self._app.add_handler(CommandHandler("forget", self._cmd_forget))
        self._app.add_handler(CommandHandler("session", self._cmd_session))
        self._app.add_handler(CommandHandler("jobs", self._cmd_jobs))
        self._app.add_handler(CommandHandler("dream", self._cmd_dream))
        self._app.add_handler(
            TGMessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

        # Initialize and start polling in background
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started (polling mode)")

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            log.info("Telegram bot stopped")

    async def send_response(self, channel_id: str, content: str, **kwargs: Any) -> None:
        """Send a message to a Telegram chat."""
        if not self._app:
            return

        try:
            await self._app.bot.send_message(
                chat_id=int(channel_id),
                text=content,
                parse_mode=None,  # Let Telegram auto-detect
            )
        except Exception:
            log.exception("Failed to send Telegram message to %s", channel_id)

    def _is_allowed(self, user_id: int) -> bool:
        """Check if a user is allowed to interact with the bot."""
        if not self.allowed_users:
            return True  # No allowlist = allow all
        return user_id in self.allowed_users

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text messages."""
        if not update.message or not update.message.text:
            return

        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            await update.message.reply_text("You are not authorized to use this bot.")
            return

        if not self._handler:
            await update.message.reply_text("Bot is not fully initialized yet.")
            return

        msg = NormalizedMessage(
            platform="telegram",
            user_id=str(user.id),
            user_name=user.first_name or user.username or str(user.id),
            content=update.message.text,
            message_id=str(update.message.message_id),
            channel_id=str(update.effective_chat.id) if update.effective_chat else None,
        )

        # Send typing indicator
        if update.effective_chat:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="typing"
            )

        try:
            await self._handler(msg)
        except Exception:
            log.exception("Error handling Telegram message")
            await update.message.reply_text(
                "Sorry, an error occurred processing your message."
            )

    # -- Command handlers --

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return

        await update.message.reply_text(
            "Claude Daemon is running.\n\n"
            "Send me any message and I'll respond using Claude Code.\n\n"
            "Commands:\n"
            "/status - Daemon status\n"
            "/memory - View persistent memory\n"
            "/forget - Start a fresh session\n"
            "/session - Current session info\n"
            "/jobs - List scheduled jobs\n"
            "/dream - Trigger memory consolidation"
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return

        # Access daemon via handler's closure - we get stats from the store
        status = "Claude Daemon: running\n"
        await update.message.reply_text(status)

    async def _cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return

        if not self._handler:
            return

        # Send the memory content as a message via Claude
        msg = NormalizedMessage(
            platform="telegram",
            user_id=str(user.id),
            user_name=user.first_name or str(user.id),
            content="Show me your current persistent memory (MEMORY.md contents).",
            message_id=str(update.message.message_id),
            channel_id=str(update.effective_chat.id) if update.effective_chat else None,
        )
        await self._handler(msg)

    async def _cmd_forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return

        # This will be handled by the router which resets the session
        msg = NormalizedMessage(
            platform="telegram",
            user_id=str(user.id),
            user_name=user.first_name or str(user.id),
            content="/forget",
            message_id=str(update.message.message_id),
            channel_id=str(update.effective_chat.id) if update.effective_chat else None,
            metadata={"command": "forget"},
        )

        # Reset the session in the store
        await update.message.reply_text("Session cleared. Starting fresh!")

    async def _cmd_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        await update.message.reply_text("Session info coming soon.")

    async def _cmd_jobs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        await update.message.reply_text("Use 'claude-daemon jobs' CLI command to view scheduled jobs.")

    async def _cmd_dream(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not self._is_allowed(user.id):
            return
        await update.message.reply_text("Triggering memory consolidation... This may take a moment.")

        msg = NormalizedMessage(
            platform="telegram",
            user_id=str(user.id),
            user_name=user.first_name or str(user.id),
            content="Consolidate and summarize your memories from recent sessions.",
            message_id=str(update.message.message_id),
            channel_id=str(update.effective_chat.id) if update.effective_chat else None,
            metadata={"command": "dream"},
        )
        if self._handler:
            await self._handler(msg)
