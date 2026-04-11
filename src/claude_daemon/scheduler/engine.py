"""SchedulerEngine - APScheduler wrapper for built-in and custom jobs."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.core.daemon import ClaudeDaemon

log = logging.getLogger(__name__)


def _parse_cron(expr: str) -> dict:
    """Parse a cron expression string into CronTrigger kwargs.

    Format: 'minute hour day month day_of_week'
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: {expr}")
    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        "day_of_week": parts[4],
    }


class SchedulerEngine:
    """Manages scheduled jobs using APScheduler."""

    def __init__(self, config: DaemonConfig, daemon: ClaudeDaemon) -> None:
        self.config = config
        self.daemon = daemon
        self._scheduler = BackgroundScheduler()
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Start the scheduler with all configured jobs."""
        self._loop = asyncio.get_event_loop()
        self._register_builtin_jobs()
        self._register_custom_jobs()
        self._scheduler.start()
        log.info("Scheduler started with %d jobs", len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        """Shutdown the scheduler gracefully."""
        self._scheduler.shutdown(wait=False)
        log.info("Scheduler stopped")

    def _run_async(self, coro_func: Callable, *args) -> None:
        """Bridge from APScheduler thread to asyncio event loop."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro_func(*args), self._loop)

    def _register_builtin_jobs(self) -> None:
        """Register the built-in daemon jobs."""

        # Auto-update: check for Claude Code updates
        self._scheduler.add_job(
            self._run_async,
            CronTrigger(**_parse_cron(self.config.update_cron)),
            args=[self._job_auto_update],
            id="auto_update",
            name="Auto-update Claude Code",
            replace_existing=True,
        )

        # Memory compaction: summarize active sessions
        self._scheduler.add_job(
            self._run_async,
            CronTrigger(**_parse_cron(self.config.compaction_cron)),
            args=[self._job_memory_compaction],
            id="memory_compaction",
            name="Memory compaction",
            replace_existing=True,
        )

        # Auto-dream: weekly memory consolidation
        if self.config.dream_enabled:
            self._scheduler.add_job(
                self._run_async,
                CronTrigger(**_parse_cron(self.config.dream_cron)),
                args=[self._job_auto_dream],
                id="auto_dream",
                name="Auto-dream memory consolidation",
                replace_existing=True,
            )

        # Session cleanup: archive old sessions
        self._scheduler.add_job(
            self._run_async,
            IntervalTrigger(hours=6),
            args=[self._job_session_cleanup],
            id="session_cleanup",
            name="Session cleanup",
            replace_existing=True,
        )

        # Heartbeat: periodic health check
        self._scheduler.add_job(
            self._run_async,
            IntervalTrigger(seconds=self.config.heartbeat_interval),
            args=[self._job_heartbeat],
            id="heartbeat",
            name="Heartbeat",
            replace_existing=True,
        )

    def _register_custom_jobs(self) -> None:
        """Register user-defined jobs from configuration."""
        for job_def in self.config.custom_jobs:
            job_id = job_def.get("id", f"custom_{len(self._scheduler.get_jobs())}")
            cron = job_def.get("cron")
            prompt = job_def.get("prompt")

            if not cron or not prompt:
                log.warning("Skipping custom job %s: missing cron or prompt", job_id)
                continue

            target_platform = job_def.get("target_platform", "cli")
            target_chat = job_def.get("target_chat_id", "")

            self._scheduler.add_job(
                self._run_async,
                CronTrigger(**_parse_cron(cron)),
                args=[self._job_custom, prompt, target_platform, target_chat],
                id=job_id,
                name=f"Custom: {job_id}",
                replace_existing=True,
            )
            log.info("Registered custom job: %s (%s)", job_id, cron)

    # -- Job implementations --

    async def _job_auto_update(self) -> None:
        if self.daemon.updater:
            await self.daemon.updater.check_and_update()

    async def _job_memory_compaction(self) -> None:
        if self.daemon.compactor:
            await self.daemon.compactor.daily_compaction()

    async def _job_auto_dream(self) -> None:
        if self.daemon.compactor:
            await self.daemon.compactor.auto_dream()

    async def _job_session_cleanup(self) -> None:
        if self.daemon.store:
            archived = self.daemon.store.cleanup_expired(self.daemon.config.max_session_age_hours)
            if archived:
                log.info("Session cleanup: archived %d expired conversations", archived)

    async def _job_heartbeat(self) -> None:
        await self.daemon.heartbeat()

    async def _job_custom(self, prompt: str, platform: str, chat_id: str) -> None:
        """Execute a custom scheduled job: send prompt to Claude, deliver result."""
        log.info("Running custom job: prompt=%s..., platform=%s", prompt[:50], platform)

        response = await self.daemon.handle_message(
            prompt=prompt,
            platform="scheduler",
            user_id="scheduler",
        )

        # If a target platform/chat is configured, send the result there
        if platform != "cli" and self.daemon.router:
            integration = self.daemon.router.integrations.get(platform)
            if integration and chat_id:
                try:
                    await integration.send_response(chat_id, response)
                except Exception:
                    log.exception("Failed to deliver custom job result to %s:%s", platform, chat_id)

    def list_jobs(self) -> list[dict]:
        """Return information about all scheduled jobs."""
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else "paused",
                "trigger": str(job.trigger),
            })
        return jobs
