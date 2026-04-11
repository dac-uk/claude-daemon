"""ClaudeDaemon - the central orchestrator.

Runs as a foreground process supervised by systemd/launchd.
Manages the ProcessManager, memory subsystem, scheduler, and integrations.
Supports both buffered and streaming response modes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import AsyncIterator

from claude_daemon.core.config import DaemonConfig
from claude_daemon.core.process import ClaudeResponse, ProcessManager
from claude_daemon.core.signals import install_signal_handlers
from claude_daemon.memory.compactor import ContextCompactor
from claude_daemon.memory.durable import DurableMemory
from claude_daemon.memory.store import ConversationStore
from claude_daemon.memory.working import WorkingMemory
from claude_daemon.scheduler.engine import SchedulerEngine
from claude_daemon.updater.updater import Updater
from claude_daemon.utils import paths as pathutil
from claude_daemon.utils.logging import setup_logging

log = logging.getLogger(__name__)


class ClaudeDaemon:
    """Central daemon orchestrator with streaming support."""

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self._shutdown_event = asyncio.Event()
        self._shutting_down = False

        # Subsystems (initialized in start())
        self.store: ConversationStore | None = None
        self.durable: DurableMemory | None = None
        self.working: WorkingMemory | None = None
        self.compactor: ContextCompactor | None = None
        self.process_manager: ProcessManager | None = None
        self.scheduler: SchedulerEngine | None = None
        self.updater: Updater | None = None
        self.router = None

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def request_shutdown(self) -> None:
        self._shutting_down = True
        self._shutdown_event.set()

    async def reload_config(self) -> None:
        try:
            self.config = DaemonConfig.load()
            log.info("Configuration reloaded successfully")
        except Exception:
            log.exception("Failed to reload configuration")

    async def start(self) -> None:
        pathutil.ensure_dirs()
        setup_logging(self.config.log_level, self.config.log_dir)

        log.info("Claude Daemon v0.2.0 starting...")
        self._write_pid()

        # Initialize subsystems
        self.store = ConversationStore(self.config.db_path)
        self.durable = DurableMemory(self.config.memory_dir)
        self.durable.ensure_soul()  # Create SOUL.md if missing
        self.working = WorkingMemory(self.store, self.durable, self.config)
        self.process_manager = ProcessManager(self.config)
        self.compactor = ContextCompactor(
            self.store, self.durable, self.process_manager, self.config
        )
        self.updater = Updater(self.config, self.process_manager)

        # Scheduler
        self.scheduler = SchedulerEngine(self.config, self)
        self.scheduler.start()

        # Signal handlers
        loop = asyncio.get_running_loop()
        install_signal_handlers(self, loop)

        # Start integrations
        await self._start_integrations()

        log.info("Claude Daemon is running (PID %d)", os.getpid())
        log.info("Data directory: %s", self.config.data_dir)

        if self.durable:
            self.durable.append_daily_log("Daemon started.")

        await self._shutdown_event.wait()
        await self.stop()

    async def stop(self) -> None:
        log.info("Shutting down...")

        if self.router:
            for name, integration in self.router.integrations.items():
                try:
                    await integration.stop()
                    log.info("Stopped integration: %s", name)
                except Exception:
                    log.exception("Error stopping integration: %s", name)

        if self.scheduler:
            self.scheduler.stop()

        if self.process_manager:
            await self.process_manager.drain_all()

        if self.durable:
            self.durable.append_daily_log("Daemon stopped gracefully.")

        if self.store:
            self.store.close()

        self._remove_pid()
        log.info("Claude Daemon stopped.")

    async def handle_message(
        self, prompt: str, session_id: str | None = None,
        platform: str = "cli", user_id: str = "local",
    ) -> str:
        """Buffered message handler - returns complete response."""
        if self._shutting_down:
            return "Claude Daemon is shutting down. Please try again later."

        assert self.store and self.process_manager and self.working and self.durable

        conv = self.store.get_or_create_conversation(
            session_id=session_id, platform=platform, user_id=user_id,
        )

        context = self.working.build_context(conv["session_id"])
        self.store.add_message(conv["id"], "user", prompt)

        response = await self.process_manager.send_message(
            prompt=prompt, session_id=conv["session_id"], system_context=context,
        )

        self.store.add_message(
            conv["id"], "assistant", response.result,
            tokens=response.output_tokens, cost=response.cost,
        )
        self.store.update_conversation(
            conv["id"], session_id=response.session_id, cost=response.cost,
        )

        if self.config.daily_log_enabled:
            summary = response.result[:200] + "..." if len(response.result) > 200 else response.result
            self.durable.append_daily_log(
                f"[{platform}:{user_id}] Q: {prompt[:100]} | A: {summary}"
            )

        # Trigger light sleep signal detection in background (non-blocking)
        if self.compactor and conv["message_count"] > 0 and conv["message_count"] % 5 == 0:
            asyncio.create_task(self._safe_light_sleep(conv["session_id"]))

        return response.result

    async def handle_message_streaming(
        self, prompt: str, session_id: str | None = None,
        platform: str = "cli", user_id: str = "local",
    ) -> AsyncIterator[str | ClaudeResponse]:
        """Streaming message handler - yields text chunks, then final ClaudeResponse."""
        if self._shutting_down:
            yield "Claude Daemon is shutting down."
            return

        assert self.store and self.process_manager and self.working and self.durable

        conv = self.store.get_or_create_conversation(
            session_id=session_id, platform=platform, user_id=user_id,
        )

        context = self.working.build_context(conv["session_id"])
        self.store.add_message(conv["id"], "user", prompt)

        accumulated = ""
        final_response = None

        async for chunk in self.process_manager.stream_message(
            prompt=prompt, session_id=conv["session_id"], system_context=context,
        ):
            if isinstance(chunk, str):
                accumulated += chunk
                yield chunk
            elif isinstance(chunk, ClaudeResponse):
                final_response = chunk
                if not accumulated and chunk.result:
                    accumulated = chunk.result

        # Store the complete response
        resp = final_response or ClaudeResponse.error("No response received")
        self.store.add_message(
            conv["id"], "assistant", accumulated or resp.result,
            tokens=resp.output_tokens, cost=resp.cost,
        )
        self.store.update_conversation(
            conv["id"], session_id=resp.session_id, cost=resp.cost,
        )

        if self.config.daily_log_enabled:
            summary = accumulated[:200] + "..." if len(accumulated) > 200 else accumulated
            self.durable.append_daily_log(
                f"[{platform}:{user_id}] Q: {prompt[:100]} | A: {summary}"
            )

        yield resp

    async def heartbeat(self) -> None:
        active = self.process_manager.active_count if self.process_manager else 0
        stats = self.store.get_stats() if self.store else {}
        log.info(
            "Heartbeat: active=%d, total_sessions=%s, total_cost=$%.2f, messages=%s",
            active,
            stats.get("total", 0),
            stats.get("total_cost", 0),
            stats.get("total_messages", 0),
        )

    async def _safe_light_sleep(self, session_id: str) -> None:
        """Run light sleep without crashing the main flow."""
        try:
            await self.compactor.light_sleep(session_id)
        except Exception:
            log.exception("Light sleep failed for session %s", session_id[:8])

    async def _start_integrations(self) -> None:
        from claude_daemon.integrations.router import MessageRouter
        self.router = MessageRouter(self)

        if self.config.telegram_token:
            try:
                from claude_daemon.integrations.telegram import TelegramIntegration
                tg = TelegramIntegration(
                    token=self.config.telegram_token,
                    allowed_users=self.config.telegram_allowed_users,
                    polling=self.config.telegram_polling,
                    daemon=self,
                )
                tg.set_message_handler(self.router.handle_incoming)
                self.router.register("telegram", tg)
                await tg.start()
                log.info("Telegram integration started")
            except ImportError:
                log.warning("Telegram not available (install claude-daemon[telegram])")
            except Exception:
                log.exception("Failed to start Telegram")

        if self.config.discord_token:
            try:
                from claude_daemon.integrations.discord_bot import DiscordIntegration
                dc = DiscordIntegration(
                    token=self.config.discord_token,
                    allowed_guilds=self.config.discord_allowed_guilds,
                    daemon=self,
                )
                dc.set_message_handler(self.router.handle_incoming)
                self.router.register("discord", dc)
                await dc.start()
                log.info("Discord integration started")
            except ImportError:
                log.warning("Discord not available (install claude-daemon[discord])")
            except Exception:
                log.exception("Failed to start Discord")

        if self.config.paperclip_url:
            try:
                from claude_daemon.integrations.paperclip import PaperclipIntegration
                pc = PaperclipIntegration(
                    url=self.config.paperclip_url,
                    api_key=self.config.paperclip_api_key or "",
                    poll_interval=self.config.paperclip_poll_interval,
                )
                pc.set_message_handler(self.router.handle_incoming)
                self.router.register("paperclip", pc)
                await pc.start()
                log.info("Paperclip integration started")
            except Exception:
                log.exception("Failed to start Paperclip")

    def _write_pid(self) -> None:
        pid_file = self.config.pid_path
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()))

    def _remove_pid(self) -> None:
        try:
            self.config.pid_path.unlink(missing_ok=True)
        except OSError:
            pass
