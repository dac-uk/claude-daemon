"""ClaudeDaemon - the central orchestrator.

Runs as a foreground process supervised by systemd/launchd.
Manages the ProcessManager, memory subsystem, scheduler, and integrations.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from claude_daemon.core.config import DaemonConfig
from claude_daemon.core.process import ProcessManager
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
    """Central daemon orchestrator."""

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self._shutdown_event = asyncio.Event()

        # Subsystems (initialized in start())
        self.store: ConversationStore | None = None
        self.durable: DurableMemory | None = None
        self.working: WorkingMemory | None = None
        self.compactor: ContextCompactor | None = None
        self.process_manager: ProcessManager | None = None
        self.scheduler: SchedulerEngine | None = None
        self.updater: Updater | None = None
        self.router = None  # set up when integrations are loaded

    def request_shutdown(self) -> None:
        """Signal the main loop to shut down."""
        self._shutdown_event.set()

    async def reload_config(self) -> None:
        """Reload configuration from disk."""
        try:
            self.config = DaemonConfig.load()
            log.info("Configuration reloaded successfully")
        except Exception:
            log.exception("Failed to reload configuration")

    async def start(self) -> None:
        """Initialize all subsystems and run the main event loop."""
        pathutil.ensure_dirs()
        setup_logging(self.config.log_level, self.config.log_dir)

        log.info("Claude Daemon v0.1.0 starting...")
        self._write_pid()

        # Initialize subsystems
        self.store = ConversationStore(self.config.db_path)
        self.durable = DurableMemory(self.config.memory_dir)
        self.working = WorkingMemory(self.store, self.durable)
        self.process_manager = ProcessManager(self.config)
        self.compactor = ContextCompactor(self.store, self.durable, self.process_manager)
        self.updater = Updater(self.config, self.process_manager)

        # Scheduler
        self.scheduler = SchedulerEngine(self.config, self)
        self.scheduler.start()

        # Install signal handlers
        loop = asyncio.get_running_loop()
        install_signal_handlers(self, loop)

        # Start integrations
        await self._start_integrations()

        log.info("Claude Daemon is running (PID %d)", os.getpid())
        log.info("Data directory: %s", self.config.data_dir)

        # Main loop - wait for shutdown signal
        await self._shutdown_event.wait()

        # Graceful shutdown
        await self.stop()

    async def stop(self) -> None:
        """Graceful shutdown: drain sessions, stop scheduler, disconnect integrations."""
        log.info("Shutting down...")

        # Stop integrations
        if self.router:
            for name, integration in self.router.integrations.items():
                try:
                    await integration.stop()
                    log.info("Stopped integration: %s", name)
                except Exception:
                    log.exception("Error stopping integration: %s", name)

        # Stop scheduler
        if self.scheduler:
            self.scheduler.stop()

        # Drain active Claude processes
        if self.process_manager:
            await self.process_manager.drain_all()

        # Flush daily log
        if self.durable:
            self.durable.append_daily_log(
                f"[{datetime.now(timezone.utc).isoformat()}] Daemon stopped gracefully."
            )

        # Close database
        if self.store:
            self.store.close()

        self._remove_pid()
        log.info("Claude Daemon stopped.")

    async def handle_message(self, prompt: str, session_id: str | None = None,
                             platform: str = "cli", user_id: str = "local") -> str:
        """Central message handler - route a prompt to Claude Code and return response."""
        assert self.store is not None
        assert self.process_manager is not None
        assert self.working is not None
        assert self.durable is not None

        # Get or create conversation
        conv = self.store.get_or_create_conversation(
            session_id=session_id,
            platform=platform,
            user_id=user_id,
        )

        # Build memory context
        context = self.working.build_context(conv["session_id"])

        # Store user message
        self.store.add_message(conv["id"], "user", prompt)

        # Send to Claude
        response = await self.process_manager.send_message(
            prompt=prompt,
            session_id=conv["session_id"],
            system_context=context,
        )

        # Store assistant response
        self.store.add_message(
            conv["id"], "assistant", response.result,
            tokens=response.output_tokens,
            cost=response.cost,
        )

        # Update conversation metadata
        self.store.update_conversation(
            conv["id"],
            session_id=response.session_id,
            cost=response.cost,
        )

        # Append to daily log
        if self.config.daily_log_enabled:
            summary = response.result[:200] + "..." if len(response.result) > 200 else response.result
            self.durable.append_daily_log(
                f"[{platform}:{user_id}] Q: {prompt[:100]}... A: {summary}"
            )

        return response.result

    async def heartbeat(self) -> None:
        """Periodic health check logged to daemon log."""
        active = self.process_manager.active_count if self.process_manager else 0
        log.info(
            "Heartbeat: active_sessions=%d, uptime=ok",
            active,
        )

    async def _start_integrations(self) -> None:
        """Initialize and start configured messaging integrations."""
        from claude_daemon.integrations.router import MessageRouter

        self.router = MessageRouter(self)

        # Telegram
        if self.config.telegram_token:
            try:
                from claude_daemon.integrations.telegram import TelegramIntegration

                tg = TelegramIntegration(
                    token=self.config.telegram_token,
                    allowed_users=self.config.telegram_allowed_users,
                    polling=self.config.telegram_polling,
                )
                tg.set_message_handler(self.router.handle_incoming)
                self.router.register("telegram", tg)
                await tg.start()
                log.info("Telegram integration started")
            except ImportError:
                log.warning("Telegram integration not available (install claude-daemon[telegram])")
            except Exception:
                log.exception("Failed to start Telegram integration")

        # Discord
        if self.config.discord_token:
            try:
                from claude_daemon.integrations.discord_bot import DiscordIntegration

                dc = DiscordIntegration(
                    token=self.config.discord_token,
                    allowed_guilds=self.config.discord_allowed_guilds,
                )
                dc.set_message_handler(self.router.handle_incoming)
                self.router.register("discord", dc)
                await dc.start()
                log.info("Discord integration started")
            except ImportError:
                log.warning("Discord integration not available (install claude-daemon[discord])")
            except Exception:
                log.exception("Failed to start Discord integration")

        # Paperclip
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
                log.exception("Failed to start Paperclip integration")

    def _write_pid(self) -> None:
        pid_file = self.config.pid_path
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()))

    def _remove_pid(self) -> None:
        try:
            self.config.pid_path.unlink(missing_ok=True)
        except OSError:
            pass
