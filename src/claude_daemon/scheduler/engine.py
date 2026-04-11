"""SchedulerEngine - APScheduler wrapper for three-phase dreaming and custom jobs."""

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
    """Parse a cron expression string into CronTrigger kwargs."""
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
    """Manages three-phase dreaming schedule and custom jobs."""

    def __init__(self, config: DaemonConfig, daemon: ClaudeDaemon) -> None:
        self.config = config
        self.daemon = daemon
        self._scheduler = BackgroundScheduler()
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._register_builtin_jobs()
        self._register_custom_jobs()
        self._scheduler.start()
        log.info("Scheduler started with %d jobs", len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        log.info("Scheduler stopped")

    def _run_async(self, coro_func: Callable, *args) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro_func(*args), self._loop)

    def _register_builtin_jobs(self) -> None:
        # Auto-update
        self._scheduler.add_job(
            self._run_async,
            CronTrigger(**_parse_cron(self.config.update_cron)),
            args=[self._job_auto_update],
            id="auto_update",
            name="Auto-update Claude Code",
            replace_existing=True,
        )

        # Phase 2: Deep Sleep (nightly consolidation)
        self._scheduler.add_job(
            self._run_async,
            CronTrigger(**_parse_cron(self.config.compaction_cron)),
            args=[self._job_deep_sleep],
            id="deep_sleep",
            name="Deep sleep (nightly consolidation)",
            replace_existing=True,
        )

        # Phase 3: REM Sleep (weekly integration)
        if self.config.dream_enabled:
            self._scheduler.add_job(
                self._run_async,
                CronTrigger(**_parse_cron(self.config.dream_cron)),
                args=[self._job_rem_sleep],
                id="rem_sleep",
                name="REM sleep (weekly integration)",
                replace_existing=True,
            )

        # Session cleanup
        self._scheduler.add_job(
            self._run_async,
            IntervalTrigger(hours=6),
            args=[self._job_session_cleanup],
            id="session_cleanup",
            name="Session cleanup",
            replace_existing=True,
        )

        # Heartbeat
        self._scheduler.add_job(
            self._run_async,
            IntervalTrigger(seconds=self.config.heartbeat_interval),
            args=[self._job_heartbeat],
            id="heartbeat",
            name="Heartbeat",
            replace_existing=True,
        )

    def _register_custom_jobs(self) -> None:
        for job_def in self.config.custom_jobs:
            job_id = job_def.get("id", f"custom_{len(self._scheduler.get_jobs())}")
            cron = job_def.get("cron")
            prompt = job_def.get("prompt")

            if not cron or not prompt:
                log.warning("Skipping custom job %s: missing cron or prompt", job_id)
                continue

            try:
                _parse_cron(cron)  # Validate at registration time
            except ValueError as e:
                log.error("Invalid cron for job %s: %s", job_id, e)
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
            result = await self.daemon.updater.check_and_update()
            log.info("Auto-update: %s", result)
            # Alert on all integrations
            if result.updated and self.daemon.router:
                for name, integ in self.daemon.router.integrations.items():
                    try:
                        for chat_id in self._get_alert_targets(name):
                            await integ.send_response(chat_id, f"Claude Code updated: {result}")
                    except Exception:
                        pass

    async def _job_deep_sleep(self) -> None:
        """Phase 2: Nightly deep sleep consolidation."""
        if self.daemon.compactor:
            try:
                await self.daemon.compactor.deep_sleep()
            except Exception:
                log.exception("Deep sleep failed")
                self._alert_failure("Deep sleep consolidation failed")

    async def _job_rem_sleep(self) -> None:
        """Phase 3: Weekly REM sleep integration."""
        if self.daemon.compactor:
            try:
                await self.daemon.compactor.rem_sleep()
            except Exception:
                log.exception("REM sleep failed")
                self._alert_failure("REM sleep integration failed")

    async def _job_session_cleanup(self) -> None:
        if self.daemon.store:
            archived = self.daemon.store.cleanup_expired(self.daemon.config.max_session_age_hours)
            if archived:
                log.info("Session cleanup: archived %d expired conversations", archived)

    async def _job_heartbeat(self) -> None:
        await self.daemon.heartbeat()

    async def _job_custom(self, prompt: str, platform: str, chat_id: str) -> None:
        log.info("Running custom job: prompt=%s..., platform=%s", prompt[:50], platform)
        response = await self.daemon.handle_message(
            prompt=prompt, platform="scheduler", user_id="scheduler",
        )
        if platform != "cli" and self.daemon.router and chat_id:
            integration = self.daemon.router.integrations.get(platform)
            if integration:
                try:
                    await integration.send_response(chat_id, response)
                except Exception:
                    log.exception("Failed to deliver custom job result")

    def _alert_failure(self, message: str) -> None:
        """Log failure and attempt to notify via daily log."""
        log.error(message)
        if self.daemon.durable:
            self.daemon.durable.append_daily_log(f"ALERT: {message}")

    def _get_alert_targets(self, platform: str) -> list[str]:
        """Get configured alert target chat IDs for a platform."""
        if platform == "telegram" and self.daemon.config.telegram_allowed_users:
            return [str(uid) for uid in self.daemon.config.telegram_allowed_users]
        return []

    def list_jobs(self) -> list[dict]:
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else "paused",
                "trigger": str(job.trigger),
            })
        return jobs
