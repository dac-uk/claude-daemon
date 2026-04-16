"""ClaudeDaemon - the central orchestrator.

Runs as a foreground process supervised by systemd/launchd.
Manages the ProcessManager, multi-agent system, memory, scheduler, and integrations.
Supports both buffered and streaming response modes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import AsyncIterator

from claude_daemon.agents.bootstrap import is_user_profile_unconfigured
from claude_daemon.agents.failure_analyzer import FailureAnalyzer
from claude_daemon.agents.improvement import ImprovementPlanner
from claude_daemon.agents.orchestrator import Orchestrator
from claude_daemon.agents.registry import AgentRegistry
from claude_daemon.agents.workflow import WorkflowEngine
from claude_daemon.core.config import DaemonConfig
from claude_daemon.core.process import ClaudeResponse, ProcessManager
from claude_daemon.core.signals import install_signal_handlers, sd_notify
from claude_daemon.memory.compactor import ContextCompactor
from claude_daemon.memory.durable import DurableMemory
from claude_daemon.memory.store import ConversationStore
from claude_daemon.memory.working import WorkingMemory
from claude_daemon.scheduler.engine import SchedulerEngine
from claude_daemon.updater.updater import Updater
from claude_daemon.utils import paths as pathutil
from claude_daemon.utils.logging import setup_logging

log = logging.getLogger(__name__)

# Platforms where messages originate from a human user (not internal agent traffic)
_HUMAN_PLATFORMS = {"cli", "telegram", "slack", "discord", "api", "paperclip"}


class ClaudeDaemon:
    """Central daemon orchestrator with multi-agent support."""

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
        self.agent_registry: AgentRegistry | None = None
        self.orchestrator: Orchestrator | None = None
        self.workflow_engine: WorkflowEngine | None = None
        self.improvement_planner: ImprovementPlanner | None = None
        self._file_watcher = None  # AgentFileWatcher (lazy import)

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def request_shutdown(self) -> None:
        self._shutting_down = True
        self._shutdown_event.set()

    async def reload_config(self) -> str:
        """Reload config, re-register custom jobs, refresh agent identities. Returns status."""
        try:
            self.config = DaemonConfig.load()
            # Refresh agent identities from disk
            if self.agent_registry:
                for agent in self.agent_registry:
                    agent.load_identity()
            if self.store:
                self.store.record_audit(action="config_reload", details="Configuration reloaded")
            log.info("Configuration reloaded successfully")
            return "Configuration reloaded. Agent identities refreshed."
        except Exception:
            log.exception("Failed to reload configuration")
            return "Failed to reload configuration — check logs."

    # -- MCP server pool management ------------------------------------------

    async def refresh_mcp(self) -> str:
        """Regenerate MCP tools.json + settings.json for all agents."""
        from claude_daemon.agents.bootstrap import refresh_agent_configs
        agents_dir = self.config.data_dir / "agents"
        counts = refresh_agent_configs(
            agents_dir,
            disabled_servers=self.config.disabled_mcp_servers,
            deny_rules=self.config.agent_deny_rules or None,
            thinking_enabled=self.config.thinking_enabled,
        )
        if self.agent_registry:
            for agent in self.agent_registry:
                agent.load_identity()
        total = sum(counts.values())
        agents = len(counts)
        avg = total // agents if agents else 0
        log.info("Agent configs refreshed: %d MCP servers across %d agents", avg, agents)
        return f"Agent configs refreshed: {avg} MCP servers active across {agents} agents."

    async def set_thinking(self, enabled: bool) -> str:
        """Toggle thinking mode and regenerate agent settings."""
        self.config.thinking_enabled = enabled
        await self.refresh_mcp()  # regenerates settings.json
        state = "enabled" if enabled else "disabled"
        log.info("Thinking %s for all agents", state)
        return f"Thinking {state} for all agents. Settings regenerated."

    async def set_default_effort(self, level: str) -> str:
        """Set the default effort level for subsequent messages."""
        valid = ("low", "medium", "high", "max", "")
        if level not in valid:
            return f"Invalid effort level: {level}. Use: {', '.join(v for v in valid if v)}"
        self.config.default_effort = level
        msg = f"Default effort set to {level}" if level else "Default effort reset to per-task-type mapping"
        log.info(msg)
        return msg

    # -- Managed Agents backend control ----------------------------------------

    async def set_managed_agents(self, enabled: bool) -> str:
        """Enable or disable Managed Agents backend for configured task types."""
        self.config.managed_agents_enabled = enabled
        state = "enabled" if enabled else "disabled"
        if enabled and self.process_manager and self.process_manager.managed:
            # Register agents if not already done
            await self._register_managed_agents()
        log.info("Managed Agents %s", state)
        return f"Managed Agents {state}. Task types: {', '.join(self.config.managed_agents_task_types)}"

    def get_managed_agents_status(self) -> dict:
        """Return status information about the Managed Agents backend."""
        if self.process_manager and self.process_manager.managed:
            return self.process_manager.managed.get_status()
        return {
            "enabled": self.config.managed_agents_enabled,
            "environment_id": None,
            "registered_agents": [],
            "agent_count": 0,
            "task_types": list(self.config.managed_agents_task_types),
            "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        }

    async def _register_managed_agents(self) -> None:
        """Register all daemon agents with the Managed Agents API."""
        if not self.process_manager or not self.process_manager.managed:
            return
        if not self.agent_registry:
            return
        managed = self.process_manager.managed
        try:
            await managed.ensure_environment()
        except Exception:
            log.exception("Failed to create Managed Agents environment — disabling")
            self.config.managed_agents_enabled = False
            return

        total = len(self.agent_registry)
        registered = 0
        failed = []
        for agent in self.agent_registry:
            try:
                await managed.register_agent(agent)
                registered += 1
            except Exception:
                failed.append(agent.name)
                log.warning("Failed to register managed agent: %s", agent.name)

        log.info("Registered %d/%d agents with Managed Agents API", registered, total)

        if failed:
            log.warning("Failed agents: %s", ", ".join(failed))

        # If majority failed, disable managed backend to avoid confusing partial state
        if total > 0 and registered < total / 2:
            log.error(
                "Majority of agents failed to register (%d/%d) — disabling Managed Agents",
                total - registered, total,
            )
            self.config.managed_agents_enabled = False

    async def enable_mcp_server(self, name: str) -> str:
        """Remove a server from the disabled list and refresh tools.json."""
        from claude_daemon.agents.bootstrap import MCP_SERVER_CATALOG
        if name not in MCP_SERVER_CATALOG:
            return f"Unknown MCP server: {name}"
        if name in self.config.disabled_mcp_servers:
            self.config.disabled_mcp_servers.remove(name)
            self._persist_disabled_mcp()
        return await self.refresh_mcp()

    async def disable_mcp_server(self, name: str) -> str:
        """Add a server to the disabled list and refresh tools.json."""
        from claude_daemon.agents.bootstrap import MCP_SERVER_CATALOG
        if name not in MCP_SERVER_CATALOG:
            return f"Unknown MCP server: {name}"
        if name not in self.config.disabled_mcp_servers:
            self.config.disabled_mcp_servers.append(name)
            self._persist_disabled_mcp()
        return await self.refresh_mcp()

    def get_mcp_status(self) -> list[dict]:
        """Return tier/status for every cataloged MCP server."""
        from claude_daemon.agents.bootstrap import get_mcp_catalog_status
        return get_mcp_catalog_status(self.config.disabled_mcp_servers)

    def _persist_disabled_mcp(self) -> None:
        """Write disabled_mcp_servers back to config.yaml."""
        import yaml
        cfg_path = self.config.data_dir / "config.yaml"
        if not cfg_path.exists():
            # Also check standard locations
            for p in [pathutil.config_dir() / "config.yaml", pathutil.config_dir() / "config.yml"]:
                if p.exists():
                    cfg_path = p
                    break

        data: dict = {}
        if cfg_path.exists():
            with open(cfg_path) as f:
                data = yaml.safe_load(f) or {}

        claude_section = data.setdefault("claude", {})
        claude_section["disabled_mcp_servers"] = list(self.config.disabled_mcp_servers)

        with open(cfg_path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        log.info("Persisted disabled_mcp_servers: %s", self.config.disabled_mcp_servers)

    async def start(self) -> None:
        pathutil.ensure_dirs()
        setup_logging(self.config.log_level, self.config.log_dir)

        from claude_daemon import __version__
        log.info("Claude Daemon v%s starting...", __version__)
        self._write_pid()

        # Initialize subsystems
        self.store = ConversationStore(self.config.db_path)
        self.durable = DurableMemory(self.config.memory_dir)
        self.durable.ensure_soul()
        self.working = WorkingMemory(self.store, self.durable, self.config)
        self.process_manager = ProcessManager(self.config)

        # Semantic memory (vector embeddings)
        from claude_daemon.memory.embeddings import EmbeddingStore
        self.embedding_store = EmbeddingStore(self.store._db, self.config)
        self.compactor = ContextCompactor(
            self.store, self.durable, self.process_manager, self.config,
            embedding_store=self.embedding_store,
        )
        self.updater = Updater(self.config, self.process_manager)

        # Multi-agent system
        from claude_daemon.agents.bootstrap import (
            create_csuite_workspaces, create_shared_workspace, refresh_agent_configs,
        )
        from claude_daemon.agents.template_merge import merge_agent_templates
        agents_dir = self.config.data_dir / "agents"
        shared_dir = self.config.data_dir / "shared"
        create_shared_workspace(self.config.data_dir)
        create_csuite_workspaces(agents_dir)
        # Merge new template sections into existing agent files (safe, idempotent)
        merge_result = merge_agent_templates(agents_dir)
        if merge_result.sections_added:
            log.info("Template merge: %s", merge_result.summary())
        # Auto-install evo plugin if enabled
        await self._ensure_evo_installed()
        # Regenerate tools.json + settings.json for all agents based on current env vars
        mcp_counts = refresh_agent_configs(
            agents_dir,
            disabled_servers=self.config.disabled_mcp_servers,
            deny_rules=self.config.agent_deny_rules or None,
            thinking_enabled=self.config.thinking_enabled,
        )
        if mcp_counts:
            sample = next(iter(mcp_counts.values()), 0)
            log.info("MCP pool: %d servers active across %d agents", sample, len(mcp_counts))
        self.agent_registry = AgentRegistry(agents_dir, shared_dir=shared_dir)
        self.agent_registry.load_all()
        self.failure_analyzer = FailureAnalyzer(
            self.process_manager, self.store, shared_dir,
        )
        self.orchestrator = Orchestrator(
            self.agent_registry, self.process_manager, self.store,
            hub=getattr(self, "_dashboard_hub", None),
            failure_analyzer=self.failure_analyzer,
            embedding_store=self.embedding_store,
        )
        self.workflow_engine = WorkflowEngine(
            self.orchestrator, self.agent_registry,
        )
        from claude_daemon.agents.discussion import DiscussionEngine
        self.discussion_engine = DiscussionEngine(
            self.orchestrator, self.agent_registry, self.store,
            self.config, shared_dir,
            hub=getattr(self, "_dashboard_hub", None),
        )
        self.orchestrator.set_discussion_engine(self.discussion_engine)
        self.orchestrator.set_workflow_engine(self.workflow_engine)
        from claude_daemon.agents.evolution import EvolutionActuator
        self.evolution_actuator = EvolutionActuator(
            self.agent_registry, self.process_manager, self.store,
            self.config, shared_dir,
        )
        self.improvement_planner = ImprovementPlanner(
            self.agent_registry, self.process_manager,
            self.store, shared_dir,
            evolution_actuator=self.evolution_actuator,
        )
        log.info("Loaded %d agents: %s",
                 len(self.agent_registry), self.agent_registry.agent_names())

        # Register agents with Managed Agents API (if enabled + API key available)
        if self.config.managed_agents_enabled and self.process_manager.managed:
            await self._register_managed_agents()

        # Agent hot-reload file watcher
        if self.config.agent_hot_reload and self.agent_registry:
            from claude_daemon.agents.watcher import AgentFileWatcher
            self._file_watcher = AgentFileWatcher(
                self.agent_registry, self.config.agent_reload_interval,
            )
            self._file_watcher.start()
            log.info("Agent hot-reload enabled (polling every %ds)", self.config.agent_reload_interval)

        # Scheduler
        self.scheduler = SchedulerEngine(self.config, self)
        self.scheduler.start()

        # Mark stale tasks from previous run as failed
        self._mark_stale_tasks()

        # Signal handlers
        loop = asyncio.get_running_loop()
        install_signal_handlers(self, loop)

        # Start integrations
        await self._start_integrations()

        log.info("Claude Daemon is running (PID %d)", os.getpid())
        log.info("Data directory: %s", self.config.data_dir)

        if self.durable:
            # Detect unclean previous shutdown (crash recovery)
            await self._detect_crash_restart()
            self.durable.append_daily_log("Daemon started.")

        # Tell systemd we're ready (no-op if not running under systemd)
        sd_notify("READY=1")

        # Proactive env health check — notify users about missing env vars
        await self._check_env_health()

        await self._shutdown_event.wait()
        await self.stop()

    async def _ensure_evo_installed(self) -> None:
        """Install the evo Claude Code plugin if evo_enabled and not already installed.

        Runs two idempotent CLI commands:
        1. claude plugin marketplace add evo-hq/evo
        2. claude plugin install evo

        Failures are logged as warnings and never block startup.
        """
        if not self.config.evo_enabled:
            return

        # Step 1: Add marketplace (idempotent — safe to run if already added)
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "plugin", "marketplace", "add", "evo-hq/evo",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode != 0:
                log.warning(
                    "Evo marketplace add failed (rc=%d): %s",
                    proc.returncode, (stderr or stdout or b"").decode()[:200],
                )
                return
        except Exception:
            log.warning("Evo marketplace add failed", exc_info=True)
            return

        # Step 2: Install plugin (idempotent — safe to run if already installed)
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "plugin", "install", "evo",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode != 0:
                log.warning(
                    "Evo plugin install failed (rc=%d): %s",
                    proc.returncode, (stderr or stdout or b"").decode()[:200],
                )
                return
        except Exception:
            log.warning("Evo plugin install failed", exc_info=True)
            return

        log.info("Evo plugin ready")

    async def stop(self) -> None:
        log.info("Shutting down...")
        sd_notify("STOPPING=1")

        # Stop HTTP API
        if hasattr(self, "_http_api") and self._http_api:
            try:
                await self._http_api.stop()
                log.info("Stopped HTTP API")
            except Exception:
                log.exception("Error stopping HTTP API")

        if self.router:
            for name, integration in self.router.integrations.items():
                try:
                    await integration.stop()
                    log.info("Stopped integration: %s", name)
                except Exception:
                    log.exception("Error stopping integration: %s", name)

        if self._file_watcher:
            self._file_watcher.stop()

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

    def _mark_stale_tasks(self) -> None:
        """Mark any pending/running tasks from a previous daemon run as failed."""
        if not self.store:
            return
        try:
            stale = self.store.get_pending_tasks()
            for task in stale:
                self.store.update_task_status(
                    task["id"], "failed",
                    error="Daemon restarted — task was interrupted",
                )
            if stale:
                log.info("Marked %d stale tasks as failed after restart", len(stale))
        except Exception:
            log.debug("Could not mark stale tasks (table may not exist yet)")

    async def _detect_crash_restart(self) -> None:
        """Check if previous shutdown was unclean (crash). Alert user if so."""
        if not self.durable:
            return
        today_log = self.durable.read_daily_log()
        if not today_log:
            return
        # If we see "Daemon started" but no subsequent "Daemon stopped gracefully",
        # the previous run crashed.
        lines = today_log.strip().split("\n")
        last_started = None
        last_stopped = None
        for i, line in enumerate(lines):
            if "Daemon started" in line:
                last_started = i
            if "Daemon stopped gracefully" in line:
                last_stopped = i
        if last_started is not None and (last_stopped is None or last_stopped < last_started):
            log.warning("Previous daemon instance did not shut down cleanly — possible crash")
            self.durable.append_daily_log("WARNING: Detected unclean previous shutdown (crash recovery)")
            if self.store:
                self.store.record_audit(
                    action="crash_detected",
                    details="Daemon restarted after unclean shutdown",
                )
            # Alert via integrations if available
            await self._alert_crash_restart()

    async def _alert_crash_restart(self) -> None:
        """Send crash restart notification to all configured alert channels."""
        message = (
            "Claude Daemon restarted after an unclean shutdown (possible crash). "
            "Check `journalctl --user -u claude-daemon` for details."
        )
        if not self.router:
            return
        for platform_name, integration in self.router.integrations.items():
            try:
                alert_targets = self._get_alert_targets(platform_name)
                for chat_id in alert_targets:
                    await integration.send_response(chat_id, message)
            except Exception:
                log.debug("Could not send crash alert to %s", platform_name)

    async def handle_message(
        self, prompt: str, session_id: str | None = None,
        platform: str = "cli", user_id: str = "local",
        agent_name: str | None = None,
        task_type: str = "chat",
    ) -> str:
        """Buffered message handler with multi-agent routing.

        If agent_name is provided, route directly to that agent.
        If prompt starts with @agent_name or /agent_name, route to that agent.
        Otherwise, the orchestrator decides.
        """
        if self._shutting_down:
            return "Claude Daemon is shutting down. Please try again later."

        assert self.store and self.process_manager and self.durable

        # Onboarding: prompt user for profile if USER.md is unconfigured
        if platform in _HUMAN_PLATFORMS and is_user_profile_unconfigured(self.config.data_dir):
            user_md_path = self.config.data_dir / "shared" / "USER.md"
            prompt = (
                "[ONBOARDING] The user hasn't set up their profile yet. "
                "Before addressing their request, briefly introduce yourself and ask for "
                "their name, role, communication style, and escalation preferences. "
                "If they provide details, write them to "
                f"'{user_md_path}' using the format:\n"
                "# User Context\\n\\nName: ...\\nRole: ...\\nStyle: ...\\nEscalation: ...\\n\n"
                "If they say 'skip', proceed normally. "
                "Their message follows.\n\n"
            ) + prompt

        # Multi-agent routing
        if self.orchestrator and self.agent_registry and len(self.agent_registry) > 0:
            agent, cleaned_prompt = self._resolve_agent(prompt, agent_name)
            response = await self.orchestrator.send_to_agent(
                agent=agent, prompt=cleaned_prompt,
                session_id=session_id, platform=platform, user_id=user_id,
                task_type=task_type,
            )

            if self.config.daily_log_enabled:
                summary = response.result[:200] + "..." if len(response.result) > 200 else response.result
                self.durable.append_daily_log(
                    f"[{agent.name}:{platform}:{user_id}] Q: {prompt[:100]} | A: {summary}"
                )

            return response.result

        # Fallback: direct send without agents (legacy path)
        assert self.working
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
        return response.result

    async def handle_message_streaming(
        self, prompt: str, session_id: str | None = None,
        platform: str = "cli", user_id: str = "local",
        agent_name: str | None = None,
    ) -> AsyncIterator[str | ClaudeResponse]:
        """Streaming handler with multi-agent routing."""
        if self._shutting_down:
            yield "Claude Daemon is shutting down."
            return

        assert self.store and self.process_manager and self.durable

        # Onboarding: prompt user for profile if USER.md is unconfigured
        if platform in _HUMAN_PLATFORMS and is_user_profile_unconfigured(self.config.data_dir):
            user_md_path = self.config.data_dir / "shared" / "USER.md"
            prompt = (
                "[ONBOARDING] The user hasn't set up their profile yet. "
                "Before addressing their request, briefly introduce yourself and ask for "
                "their name, role, communication style, and escalation preferences. "
                "If they provide details, write them to "
                f"'{user_md_path}' using the format:\n"
                "# User Context\\n\\nName: ...\\nRole: ...\\nStyle: ...\\nEscalation: ...\\n\n"
                "If they say 'skip', proceed normally. "
                "Their message follows.\n\n"
            ) + prompt

        # Multi-agent streaming
        if self.orchestrator and self.agent_registry and len(self.agent_registry) > 0:
            agent, cleaned_prompt = self._resolve_agent(prompt, agent_name)

            async for chunk in self.orchestrator.stream_to_agent(
                agent=agent, prompt=cleaned_prompt,
                session_id=session_id, platform=platform, user_id=user_id,
            ):
                yield chunk

            if self.config.daily_log_enabled:
                self.durable.append_daily_log(
                    f"[{agent.name}:{platform}:{user_id}] Streamed: {prompt[:100]}"
                )
            return

        # Fallback: direct stream without agents
        assert self.working
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

        resp = final_response or ClaudeResponse.error("No response received")
        self.store.add_message(
            conv["id"], "assistant", accumulated or resp.result,
            tokens=resp.output_tokens, cost=resp.cost,
        )
        self.store.update_conversation(
            conv["id"], session_id=resp.session_id, cost=resp.cost,
        )
        yield resp

    def _resolve_agent(self, prompt: str, agent_name: str | None = None):
        """Resolve which agent handles a message. Returns (agent, cleaned_prompt)."""
        from claude_daemon.agents.agent import Agent

        # Explicit agent name
        if agent_name:
            agent = self.agent_registry.get(agent_name)
            if agent:
                return agent, prompt

        # Check for @agent or /agent addressing in prompt
        agent, cleaned = self.orchestrator.resolve_agent(prompt)
        if agent:
            return agent, cleaned

        # Default to orchestrator (which may auto-route for complex messages)
        orchestrator = self.agent_registry.get_orchestrator()
        if orchestrator:
            return orchestrator, prompt

        # Last resort
        agents = self.agent_registry.list_agents()
        return agents[0] if agents else Agent(name="default", workspace=self.config.data_dir), prompt

    # -- Dynamic Agent Management (callable from chat) --

    @staticmethod
    def _sanitize_agent_name(name: str) -> str | None:
        """Sanitize agent name to prevent path traversal. Returns None if invalid."""
        import re
        name = name.lower().replace(" ", "-")
        if not re.match(r'^[a-z0-9_-]{1,30}$', name):
            return None
        return name

    def create_agent(self, name: str, role: str = "", emoji: str = "",
                     model: str = "sonnet", soul: str = "") -> str:
        """Create a new agent dynamically. Returns status message."""
        if not self.agent_registry:
            return "Agent registry not initialized."
        name = self._sanitize_agent_name(name)
        if name is None:
            return "Invalid agent name. Use only lowercase letters, numbers, hyphens, underscores (max 30 chars)."
        if self.agent_registry.get(name):
            return f"Agent '{name}' already exists."
        agent = self.agent_registry.create_agent(
            name=name, role=role, emoji=emoji, is_orchestrator=False,
        )
        # Write model config to IDENTITY.md
        id_path = agent.workspace / "IDENTITY.md"
        id_path.write_text(
            f"# Identity\n\nName: {name}\nRole: {role}\nEmoji: {emoji}\n"
            f"Model: {model}\nPlanning-Model: opus\nChat-Model: {model}\nScheduled-Model: haiku\n"
        )
        if soul:
            (agent.workspace / "SOUL.md").write_text(soul)
        agent.load_identity()
        if self.store:
            self.store.record_audit(
                action="agent_create", agent_name=name,
                details=f"role={role}, model={model}",
            )
        return f"Created agent {agent.identity.display_name} ({role}) using {model}"

    def update_agent(self, name: str, field: str, value: str) -> str:
        """Update a field on an existing agent. Fields: role, emoji, model, soul."""
        if not self.agent_registry:
            return "Agent registry not initialized."
        agent = self.agent_registry.get(name.lower())
        if not agent:
            return f"Agent '{name}' not found."

        field = field.lower()
        if field == "soul":
            (agent.workspace / "SOUL.md").write_text(value)
        elif field in ("role", "emoji", "model", "planning-model", "chat-model", "scheduled-model"):
            id_path = agent.workspace / "IDENTITY.md"
            content = id_path.read_text() if id_path.exists() else ""
            # Update or append the field
            lines = content.split("\n")
            updated = False
            for i, line in enumerate(lines):
                if line.strip().lower().startswith(f"{field}:"):
                    lines[i] = f"{field.title()}: {value}"
                    updated = True
                    break
            if not updated:
                lines.append(f"{field.title()}: {value}")
            id_path.write_text("\n".join(lines))
        elif field == "rules":
            (agent.workspace / "AGENTS.md").write_text(value)
        else:
            return f"Unknown field '{field}'. Use: role, emoji, model, soul, rules"

        agent.load_identity()
        if self.store:
            self.store.record_audit(
                action="agent_update", agent_name=name.lower(),
                details=f"field={field}, value={value[:100]}",
            )
        return f"Updated {name}.{field} = {value[:50]}{'...' if len(value) > 50 else ''}"

    def spawn_task(self, agent_name: str, prompt: str) -> str:
        """Spawn a background task on an agent. Returns immediately."""
        if not self.orchestrator or not self.agent_registry:
            return "Not initialized."
        agent = self.agent_registry.get(agent_name.lower())
        if not agent:
            return f"Agent '{agent_name}' not found."
        task = self.orchestrator.spawn_task(agent, prompt)
        return f"Spawned task {task.task_id} on {agent.identity.display_name}"

    def list_tasks(self) -> str:
        """List all spawned tasks and their status."""
        if not self.orchestrator:
            return "Not initialized."
        tasks = self.orchestrator.list_tasks()
        if not tasks:
            return "No active tasks."
        lines = ["Tasks:\n"]
        for t in tasks[-20:]:
            lines.append(
                f"  {t.task_id} [{t.status}] {t.agent_name}: {t.prompt[:60]}"
                + (f" (${t.cost:.4f})" if t.cost else "")
            )
        return "\n".join(lines)

    def delete_agent(self, name: str) -> str:
        """Remove an agent from the registry (workspace files preserved)."""
        if not self.agent_registry:
            return "Agent registry not initialized."
        if self.agent_registry.remove_agent(name.lower()):
            if self.store:
                self.store.record_audit(action="agent_delete", agent_name=name.lower())
            return f"Agent '{name}' removed from registry. Workspace files preserved at agents/{name}/"
        return f"Agent '{name}' not found."

    async def run_build_workflow(
        self, request: str, max_total_cost: float = 0.0,
    ) -> str:
        """Run the 2-stage build quality gate workflow.

        Albert builds backend → Luna builds UI → Max reviews → retry on failure.
        """
        if not self.workflow_engine or not self.agent_registry:
            return "Workflow engine not initialized."

        if self.store:
            self.store.record_audit(
                action="workflow_start", details=f"request={request[:200]}"
            )

        from claude_daemon.agents.workflow import WorkflowStep

        build_steps = []
        if self.agent_registry.get("albert"):
            build_steps.append(WorkflowStep(
                agent_name="albert",
                prompt_template=(
                    "Build the backend for this request: {original_request}\n\n"
                    "Implement the core logic, data models, and API endpoints needed."
                ),
                label="backend",
            ))
        if self.agent_registry.get("luna"):
            build_steps.append(WorkflowStep(
                agent_name="luna",
                prompt_template=(
                    "Build the UI for this request: {original_request}\n\n"
                    "Previous backend work:\n{prev_result}\n\n"
                    "Create the views, layouts, and visual components."
                ),
                label="frontend",
            ))

        if not build_steps:
            return "No build agents (albert/luna) found."

        reviewer = self.agent_registry.get("max")
        if reviewer:
            review_step = WorkflowStep(
                agent_name="max",
                prompt_template=(
                    "Review this build for quality. Original request: {original_request}\n\n"
                    "Build output:\n{build_output}\n\n"
                    "Check functional correctness, code quality, and completeness. "
                    "Respond with PASS if acceptable, or FAIL with specific issues."
                ),
                label="review",
            )
            result = await self.workflow_engine.execute_review_loop(
                build_steps, review_step, request, max_iterations=3,
                max_total_cost=max_total_cost,
            )
        else:
            result = await self.workflow_engine.execute_pipeline(
                build_steps, request, max_total_cost=max_total_cost,
            )

        if self.store:
            self.store.record_audit(
                action="workflow_complete",
                details=f"success={result.success}, steps={len(result.steps)}, cost=${result.total_cost:.4f}",
                cost_usd=result.total_cost, success=result.success,
            )

        summary = f"Workflow {'PASSED' if result.success else 'FAILED'}\n"
        summary += result.summary()
        summary += f"\n\nFinal output:\n{result.final_result[:2000]}"
        return summary

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

    async def _start_integrations(self) -> None:
        from claude_daemon.integrations.router import MessageRouter
        self.router = MessageRouter(self)

        startup_timeout = 30  # seconds per integration

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
                await asyncio.wait_for(tg.start(), timeout=startup_timeout)
                log.info("Telegram integration started")
            except ImportError:
                log.warning("Telegram not available (install claude-daemon[telegram])")
            except asyncio.TimeoutError:
                log.error("Telegram startup timed out after %ds — skipping", startup_timeout)
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
                await asyncio.wait_for(dc.start(), timeout=startup_timeout)
                log.info("Discord integration started")
            except ImportError:
                log.warning("Discord not available (install claude-daemon[discord])")
            except asyncio.TimeoutError:
                log.error("Discord startup timed out after %ds — skipping", startup_timeout)
            except Exception:
                log.exception("Failed to start Discord")

        if self.config.paperclip_url:
            try:
                from claude_daemon.integrations.paperclip import PaperclipIntegration
                pc = PaperclipIntegration(
                    url=self.config.paperclip_url,
                    api_key=self.config.paperclip_api_key or "",
                    poll_interval=self.config.paperclip_poll_interval,
                    task_limit=self.config.paperclip_task_limit,
                    startup_timeout=self.config.paperclip_startup_timeout,
                )
                pc.set_message_handler(self.router.handle_incoming)
                self.router.register("paperclip", pc)
                await pc.start()
                log.info("Paperclip integration started")
            except Exception:
                log.exception("Failed to start Paperclip")

        if self.config.api_enabled:
            try:
                from claude_daemon.integrations.http_api import HttpApi
                self._http_api = HttpApi(
                    daemon=self,
                    port=self.config.api_port,
                    api_key=self.config.api_key,
                )
                await self._http_api.start()
                log.info("HTTP API started on port %d", self.config.api_port)

                # Wire the dashboard hub to the orchestrator for live events
                if self.orchestrator and self._http_api.hub:
                    self.orchestrator.hub = self._http_api.hub
            except ImportError:
                log.warning("aiohttp not available (pip install aiohttp)")
            except Exception:
                log.exception("Failed to start HTTP API")

    async def _check_env_health(self) -> None:
        """Check for missing env vars and notify users via available channels.

        Always writes warnings to shared/WARNINGS.md so they're visible via
        'claude-daemon status' and 'claude-daemon chat' even if no messaging
        channel is configured yet.
        """
        if not self.agent_registry:
            return

        from claude_daemon.core.env_manager import get_missing_env_report

        report = get_missing_env_report(self.agent_registry)
        warnings_path = self.config.data_dir / "shared" / "WARNINGS.md"
        warnings_path.parent.mkdir(parents=True, exist_ok=True)

        if not report:
            log.info("Env health check: all MCP tool env vars configured")
            # Clear warnings file if no issues
            if warnings_path.exists():
                warnings_path.unlink()
            return

        log.warning("Env health check:\n%s", report)

        # Always write to file — visible via 'claude-daemon status' and 'chat'
        warnings_path.write_text(
            "# Active Warnings\n\n"
            "These warnings were generated at daemon startup. "
            "Fix the issues below, then restart the daemon to clear them.\n\n"
            f"{report}\n"
        )

        # Proactively notify users via any available integration
        if self.router:
            for platform_name, integration in self.router.integrations.items():
                targets = self._get_alert_targets(platform_name)
                for chat_id in targets:
                    try:
                        await integration.send_response(chat_id, report)
                    except Exception:
                        log.warning(
                            "Failed to deliver env health report via %s:%s",
                            platform_name, chat_id,
                        )

    def _get_alert_targets(self, platform: str) -> list[str]:
        """Get alert target chat/channel IDs for a platform."""
        if platform == "telegram" and self.config.telegram_allowed_users:
            return [str(uid) for uid in self.config.telegram_allowed_users]
        if platform == "discord" and self.config.discord_alert_channel_ids:
            return list(self.config.discord_alert_channel_ids)
        return []

    def _write_pid(self) -> None:
        pid_file = self.config.pid_path
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()))

    def _remove_pid(self) -> None:
        try:
            self.config.pid_path.unlink(missing_ok=True)
        except OSError:
            pass
