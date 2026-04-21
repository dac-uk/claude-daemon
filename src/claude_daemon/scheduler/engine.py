"""SchedulerEngine - APScheduler wrapper for three-phase dreaming and custom jobs."""

from __future__ import annotations

import asyncio
import logging
import uuid
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
        self._failure_counts: dict[str, int] = {}  # job_id -> consecutive failure count
        self._max_failures: int = 3  # pause job after this many consecutive failures
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._load_failure_counts()

    def start(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._register_builtin_jobs()
        self._register_custom_jobs()
        self._register_agent_heartbeats()
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

        # Log retention (daily at 3:30 AM)
        self._scheduler.add_job(
            self._run_async,
            CronTrigger(hour=3, minute=30),
            args=[self._job_log_retention],
            id="log_retention",
            name="Log retention cleanup",
            replace_existing=True,
        )

        # Systemd watchdog ping (every 60s — WatchdogSec=120 in service file)
        self._scheduler.add_job(
            self._job_watchdog_ping,
            IntervalTrigger(seconds=60),
            id="watchdog_ping",
            name="Systemd watchdog ping",
            replace_existing=True,
        )

    def _register_agent_heartbeats(self) -> None:
        """Parse each agent's HEARTBEAT.md and register their tasks as cron jobs."""
        if not self.daemon.agent_registry:
            return

        count = 0
        for agent in self.daemon.agent_registry:
            tasks = agent.parse_heartbeat_tasks()
            for task in tasks:
                job_id = f"heartbeat:{agent.name}:{task.title.lower().replace(' ', '_')[:30]}"
                try:
                    cron_kwargs = _parse_cron(task.cron)
                except ValueError as e:
                    log.error("Invalid cron in %s HEARTBEAT.md: %s", agent.name, e)
                    continue

                self._scheduler.add_job(
                    self._run_async,
                    CronTrigger(**cron_kwargs),
                    args=[self._job_agent_heartbeat, agent.name, task.prompt, task.model],
                    id=job_id,
                    name=f"Heartbeat: {agent.name} - {task.title}",
                    replace_existing=True,
                )
                count += 1

        if count:
            log.info("Registered %d agent heartbeat tasks", count)

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

            # drain_all() killed the SDK bridge — restart it and re-warm sessions
            if self.daemon.process_manager:
                try:
                    await self.daemon.process_manager.ensure_sdk_bridge()
                except Exception:
                    log.warning("Failed to restart SDK bridge after update")
                try:
                    await self.daemon._precreate_agent_sessions()
                except Exception:
                    log.warning("Failed to re-warm sessions after update")

            # Self-update the daemon's own code alongside Claude CLI
            try:
                self_result = await self.daemon.updater.self_update()
                if self_result.updated:
                    log.info("Daemon self-update: %s", self_result)
            except Exception:
                log.exception("Daemon self-update failed")

            # Alert on all integrations
            if result.updated and self.daemon.router:
                await self._send_webhook_alerts(
                    "update", f"Claude Code updated: {result}",
                )
                for name, integ in self.daemon.router.integrations.items():
                    for chat_id in self._get_alert_targets(name):
                        try:
                            await integ.send_response(chat_id, f"Claude Code updated: {result}")
                        except Exception:
                            log.warning("Failed to deliver update notification via %s:%s", name, chat_id)

    async def _job_deep_sleep(self) -> None:
        """Phase 2: Nightly deep sleep consolidation + per-agent compaction."""
        if self.daemon.compactor:
            try:
                await self.daemon.compactor.deep_sleep()
            except Exception:
                log.exception("Deep sleep failed")
                self._alert_failure("Deep sleep consolidation failed")

            # Per-agent memory compaction
            if self.daemon.agent_registry:
                try:
                    await self.daemon.compactor.compact_all_agent_memories(
                        self.daemon.agent_registry,
                    )
                except Exception:
                    log.exception("Per-agent memory compaction failed")

    async def _job_rem_sleep(self) -> None:
        """Phase 3: Weekly REM sleep integration + improvement cycle."""
        if self.daemon.compactor:
            try:
                await self.daemon.compactor.rem_sleep()
            except Exception:
                log.exception("REM sleep failed")
                self._alert_failure("REM sleep integration failed")

        # Per-agent self-assessments (update each agent's REFLECTIONS.md)
        if self.daemon.improvement_planner:
            try:
                await self.daemon.improvement_planner.run_all_self_assessments()
                log.info("Per-agent self-assessments complete")
            except Exception:
                log.exception("Per-agent self-assessments failed")

            # Weekly improvement cycle (synthesise learnings, generate plan)
            try:
                plan = await self.daemon.improvement_planner.run_weekly_improvement_cycle()
                log.info("Improvement cycle complete")

                # Deliver improvement suggestions to user via all integrations
                if plan and self.daemon.router:
                    summary = "Weekly Improvement Plan:\n\n" + plan[:3000]
                    for platform_name, integration in self.daemon.router.integrations.items():
                        for chat_id in self._get_alert_targets(platform_name):
                            try:
                                await integration.send_response(chat_id, summary)
                            except Exception:
                                log.warning("Failed to deliver improvement plan via %s:%s", platform_name, chat_id)
            except Exception:
                log.exception("Improvement cycle failed")

    async def _job_session_cleanup(self) -> None:
        if self.daemon.store:
            archived = self.daemon.store.cleanup_expired(self.daemon.config.max_session_age_hours)
            if archived:
                log.info("Session cleanup: archived %d expired conversations", archived)

    async def _job_heartbeat(self) -> None:
        await self.daemon.heartbeat()
        if self.config.shared_brain_enabled and self.daemon.agent_registry:
            try:
                from claude_daemon.agents.shared_brain import SharedBrainBuilder
                SharedBrainBuilder(
                    registry=self.daemon.agent_registry,
                    shared_dir=self.config.data_dir / "shared",
                    output_path=self.config.shared_brain_path,
                    max_chars=self.config.shared_brain_max_chars,
                ).write()
            except Exception:
                log.exception("Shared brain regen failed")

    async def _job_agent_heartbeat(
        self, agent_name: str, prompt: str, model: str,
    ) -> None:
        """Execute a single agent heartbeat task and deliver results."""
        if not self.daemon.orchestrator or not self.daemon.agent_registry:
            return

        agent = self.daemon.agent_registry.get(agent_name)
        if not agent:
            log.warning("Heartbeat: agent '%s' not found", agent_name)
            return

        # Use agent's configured model for scheduled tasks (authoritative)
        effective_model = agent.get_model("scheduled")

        # Circuit breaker: skip if this heartbeat has failed too many times
        job_key = f"{agent_name}:{effective_model}"
        if self._failure_counts.get(job_key, 0) >= self._max_failures:
            log.warning("Heartbeat %s circuit-broken after %d consecutive failures — skipping", agent_name, self._max_failures)
            return

        # Serialize concurrent heartbeats for the same agent
        lock = self._agent_locks.setdefault(agent_name, asyncio.Lock())
        if lock.locked():
            log.info("Heartbeat %s: already running, queuing", agent_name)
        await lock.acquire()
        try:
            await self._do_agent_heartbeat(agent, agent_name, prompt, effective_model, job_key)
        finally:
            lock.release()

    async def _do_agent_heartbeat(
        self, agent, agent_name: str, prompt: str, model: str, job_key: str,
    ) -> None:
        """Inner heartbeat logic, called under agent lock."""
        log.info("Heartbeat: running '%s' task (model=%s)", agent_name, model)

        # Create task_queue row so heartbeat work appears on the Operations
        # audit trail (autonomous agent actions need to be visible).
        task_id = uuid.uuid4().hex
        if self.daemon.store:
            try:
                self.daemon.store.create_task(
                    task_id=task_id, agent_name=agent_name, prompt=prompt,
                    task_type="heartbeat", platform="heartbeat",
                    user_id="scheduler", initial_status="running",
                )
            except Exception:
                log.exception("Failed to create heartbeat task_queue row for %s", agent_name)

        try:
            response = await self.daemon.orchestrator.send_to_agent(
                agent=agent,
                prompt=prompt,
                platform="heartbeat",
                user_id="scheduler",
                task_type="scheduled",
            )
            if response.is_error:
                self._failure_counts[job_key] = self._failure_counts.get(job_key, 0) + 1
                self._save_failure_counts()
                log.error("Heartbeat %s failed (%d/%d): %s", agent_name, self._failure_counts[job_key], self._max_failures, response.result[:200])
                if self._failure_counts[job_key] >= self._max_failures:
                    self._alert_failure(f"Heartbeat for {agent_name} circuit-broken after {self._max_failures} consecutive failures")
                if self.daemon.store:
                    try:
                        self.daemon.store.update_task_status(
                            task_id, "failed",
                            error=response.result[:500], cost_usd=response.cost,
                            session_id=response.session_id,
                        )
                    except Exception:
                        log.exception("Failed to mark heartbeat task failed")
                return

            # Reset failure counter on success
            if job_key in self._failure_counts:
                self._failure_counts.pop(job_key, None)
                self._save_failure_counts()
            log.info("Heartbeat %s complete: cost=$%.4f", agent_name, response.cost)

            # Audit log
            if self.daemon.store:
                self.daemon.store.record_audit(
                    action="heartbeat_execute", agent_name=agent_name,
                    details=f"model={model}", cost_usd=response.cost,
                    success=not response.is_error,
                )

            # Record metric
            if self.daemon.store:
                self.daemon.store.record_agent_metric(
                    agent_name=agent_name, metric_type="heartbeat",
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cost_usd=response.cost, model=model,
                    platform="heartbeat", success=True,
                )

            # Log to daily log
            if self.daemon.durable:
                self.daemon.durable.append_daily_log(
                    f"[heartbeat:{agent_name}] {response.result[:200]}"
                )

            # Log to shared event log for inter-agent awareness
            self._write_event(agent_name, "heartbeat", response.result[:500])

            # Deliver result to all configured alert targets
            if self.daemon.router and response.result.strip():
                display = agent.identity.display_name
                text = f"[{display} heartbeat]\n\n{response.result}"
                for platform_name, integration in self.daemon.router.integrations.items():
                    for chat_id in self._get_alert_targets(platform_name):
                        try:
                            await integration.send_response(chat_id, text)
                        except Exception:
                            log.warning("Failed to deliver heartbeat result for %s via %s:%s", agent_name, platform_name, chat_id)

            # Mark task_queue row completed so Operations shows the audit trail
            if self.daemon.store:
                try:
                    self.daemon.store.update_task_status(
                        task_id, "completed",
                        result=response.result[:2000], cost_usd=response.cost,
                        session_id=response.session_id,
                    )
                except Exception:
                    log.exception("Failed to mark heartbeat task completed")

        except Exception as exc:
            log.exception("Heartbeat task failed for %s", agent_name)
            if self.daemon.store:
                try:
                    self.daemon.store.update_task_status(
                        task_id, "failed", error=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    log.exception("Failed to mark heartbeat task failed after exception")

    async def _job_log_retention(self) -> None:
        """Delete daily log files older than log_retention_days."""
        if not self.daemon.durable:
            return
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.daemon.config.log_retention_days)
        memory_dir = self.daemon.config.memory_dir
        if not memory_dir.is_dir():
            return
        removed = 0
        for log_file in memory_dir.glob("*.md"):
            try:
                # Daily logs are named like 2024-01-15.md
                date_str = log_file.stem
                file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if file_date < cutoff:
                    log_file.unlink()
                    removed += 1
            except (ValueError, OSError):
                continue
        if removed:
            log.info("Log retention: removed %d log files older than %d days", removed, self.daemon.config.log_retention_days)

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

    def _job_watchdog_ping(self) -> None:
        """Pet the systemd watchdog to prevent restart. Synchronous (not async)."""
        from claude_daemon.core.signals import sd_notify
        sd_notify("WATCHDOG=1")

    def _write_event(self, agent_name: str, event_type: str, summary: str) -> None:
        """Write to the shared event log so other agents can see activity."""
        if not self.daemon.config:
            return
        events_file = self.daemon.config.data_dir / "shared" / "events.md"
        events_file.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        entry = f"- [{ts}] **{agent_name}** ({event_type}): {summary[:200]}\n"

        # Keep event log bounded (last 100 entries)
        lines = []
        if events_file.exists():
            lines = events_file.read_text().split("\n")
        lines.append(entry.strip())
        if len(lines) > 100:
            lines = lines[-100:]
        events_file.write_text("# Agent Events\n\n" + "\n".join(lines) + "\n")

    def _alert_failure(self, message: str) -> None:
        """Log failure and attempt to notify via daily log and webhooks."""
        log.error(message)
        if self.daemon.durable:
            self.daemon.durable.append_daily_log(f"ALERT: {message}")
        if self.daemon.store:
            self.daemon.store.record_audit(
                action="alert_failure", details=message, success=False,
            )
        # Outbound webhook alerts
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._send_webhook_alerts("failure", message), self._loop,
            )

    async def _send_webhook_alerts(
        self, event_type: str, message: str, metadata: dict | None = None,
    ) -> None:
        """POST alert payload to all configured webhook URLs (fire-and-forget)."""
        urls = self.config.alert_webhook_urls
        if not urls:
            return
        from datetime import datetime, timezone
        import httpx
        payload: dict = {
            "event": event_type,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "claude-daemon",
        }
        if metadata:
            payload["metadata"] = metadata
        timeout = self.config.alert_webhook_timeout
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for url in urls:
                    try:
                        resp = await client.post(url, json=payload)
                        if resp.status_code >= 400:
                            log.warning("Webhook alert to %s returned %d", url, resp.status_code)
                        else:
                            log.debug("Webhook alert delivered to %s", url)
                    except Exception:
                        log.warning("Failed to deliver webhook alert to %s", url, exc_info=True)
        except Exception:
            log.warning("Failed to create HTTP client for webhook alerts", exc_info=True)

    def _get_alert_targets(self, platform: str) -> list[str]:
        """Get configured alert target chat/channel IDs for a platform."""
        if platform == "telegram" and self.daemon.config.telegram_allowed_users:
            return [str(uid) for uid in self.daemon.config.telegram_allowed_users]
        if platform == "discord" and self.daemon.config.discord_alert_channel_ids:
            return list(self.daemon.config.discord_alert_channel_ids)
        return []

    def _load_failure_counts(self) -> None:
        """Load persisted circuit breaker failure counts from disk."""
        import json as _json
        path = self.config.data_dir / ".circuit_breaker.json"
        if path.exists():
            try:
                self._failure_counts = _json.loads(path.read_text())
                if self._failure_counts:
                    log.info("Loaded circuit breaker state: %d entries", len(self._failure_counts))
            except Exception:
                log.warning("Failed to load circuit breaker state — resetting")
                self._failure_counts = {}

    def _save_failure_counts(self) -> None:
        """Persist circuit breaker failure counts to disk."""
        import json as _json
        path = self.config.data_dir / ".circuit_breaker.json"
        try:
            path.write_text(_json.dumps(self._failure_counts))
        except Exception:
            log.warning("Failed to persist circuit breaker state")

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
