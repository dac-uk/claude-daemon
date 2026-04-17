"""Native task API — submit, cancel, look up tasks.

Thin layer over Orchestrator.spawn_task() + ConversationStore.get_task() that
adds external-facing semantics: pre-created DB rows (so callers get a task_id
before the worker starts), uniform response shape, optional metadata.

Designed to be called from HTTP handlers and from the Paperclip compat shim.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_daemon.agents.orchestrator import Orchestrator, SpawnedTask
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.memory.store import ConversationStore
    from claude_daemon.orchestration.budgets import BudgetStore

log = logging.getLogger(__name__)


@dataclass
class TaskSubmission:
    """Request shape for TaskAPI.submit_task()."""

    prompt: str
    agent: str | None = None
    user_id: str = "api"
    task_type: str = "default"
    platform: str = "api"
    goal_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskSubmissionResult:
    """Response shape for TaskAPI.submit_task()."""

    task_id: str
    status: str  # pending | running | rejected | error
    agent: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"task_id": self.task_id, "status": self.status}
        if self.agent:
            out["agent"] = self.agent
        if self.error:
            out["error"] = self.error
        return out


class TaskAPI:
    """Public-facing task submission/management API.

    Wraps Orchestrator + store so external callers (HTTP clients, Paperclip
    heartbeat compat shim) can submit tasks uniformly.
    """

    def __init__(
        self,
        orchestrator: Orchestrator,
        registry: AgentRegistry,
        store: ConversationStore,
        budget_store: BudgetStore | None = None,
    ) -> None:
        self._orch = orchestrator
        self._registry = registry
        self._store = store
        self._budget_store = budget_store

    def _resolve_agent(self, name: str | None) -> tuple[str | None, Any]:
        """Resolve an agent name via the registry. Returns (name, agent) or (None, None)."""
        if not name:
            # Default: pick the orchestrator agent if tagged, else first agent
            orch = self._registry.get_orchestrator()
            if orch is not None:
                return orch.name, orch
            agents = self._registry.list_agents()
            if agents:
                return agents[0].name, agents[0]
            return None, None
        agent = self._registry.get(name)
        if agent is None:
            return None, None
        return agent.name, agent

    def submit_task(self, req: TaskSubmission) -> TaskSubmissionResult:
        """Submit a task for async execution. Returns immediately with task_id."""
        if not req.prompt or not req.prompt.strip():
            return TaskSubmissionResult(
                task_id="", status="rejected", error="Empty prompt",
            )

        agent_name, agent = self._resolve_agent(req.agent)
        if agent is None:
            return TaskSubmissionResult(
                task_id="", status="rejected",
                error=f"Unknown agent: {req.agent}",
            )

        # Budget enforcement (Phase 2)
        if self._budget_store is not None:
            from claude_daemon.orchestration.enforcement import enforce_budget
            decision = enforce_budget(
                self._budget_store,
                agent_name=agent_name,
                user_id=req.user_id,
                task_type=req.task_type,
            )
            if not decision.allowed:
                return TaskSubmissionResult(
                    task_id="", status=decision.outcome,
                    agent=agent_name, error=decision.reason,
                )

        # Generate task_id up-front so caller gets it synchronously
        task_id = str(uuid.uuid4())[:12]

        # Persist metadata as JSON string in a dedicated column (added in migration)
        metadata_json = json.dumps(req.metadata) if req.metadata else None

        try:
            self._store.create_task(
                task_id, agent_name, req.prompt[:2000],
                task_type=req.task_type, platform=req.platform, user_id=req.user_id,
                metadata=metadata_json, goal_id=req.goal_id,
            )
        except Exception:
            log.exception("Could not persist task %s to DB", task_id)
            return TaskSubmissionResult(
                task_id=task_id, status="error", agent=agent_name,
                error="DB persistence failed",
            )

        # Hand off to orchestrator. spawn_task() accepts a pre-generated task_id.
        try:
            self._orch.spawn_task(
                agent=agent,
                prompt=req.prompt,
                platform=req.platform,
                user_id=req.user_id,
                task_type=req.task_type,
                task_id=task_id,
            )
        except Exception:
            log.exception("Failed to spawn task %s", task_id)
            try:
                self._store.update_task_status(
                    task_id, "failed", error="spawn failed",
                )
            except Exception:
                pass
            return TaskSubmissionResult(
                task_id=task_id, status="error", agent=agent_name,
                error="Spawn failed",
            )

        return TaskSubmissionResult(
            task_id=task_id, status="pending", agent=agent_name,
        )

    def get_task(self, task_id: str) -> dict | None:
        """Look up a task by id. Returns DB row merged with live state if running."""
        row = self._store.get_task(task_id)
        if row is None:
            return None
        # Merge live state from orchestrator if the task is still active
        spawned = self._orch._spawned_tasks.get(task_id)
        if spawned:
            row["live_status"] = spawned.status
            row["live_cost"] = spawned.cost
        return row

    def list_pending(self, agent: str | None = None, limit: int = 50) -> list[dict]:
        """List pending + running tasks (DB truth)."""
        rows = self._store.get_pending_tasks()
        if agent:
            rows = [r for r in rows if r.get("agent_name") == agent]
        return rows[:limit]

    def list_recent(self, limit: int = 50) -> list[dict]:
        """List most-recent tasks regardless of status."""
        return self._store.get_recent_tasks(limit=limit)

    async def cancel_task(self, task_id: str) -> dict:
        """Cancel a pending or running task.

        Cancels the asyncio future if alive, updates DB row, and broadcasts.
        Returns {"task_id", "status", "cancelled": bool}.
        """
        row = self._store.get_task(task_id)
        if row is None:
            return {"task_id": task_id, "status": "unknown", "cancelled": False}

        spawned = self._orch._spawned_tasks.get(task_id)
        cancelled = False
        if spawned and spawned._future and not spawned._future.done():
            spawned._future.cancel()
            spawned.status = "cancelled"
            cancelled = True

        try:
            self._store.update_task_status(task_id, "cancelled")
        except Exception:
            log.exception("Failed to mark task %s cancelled in DB", task_id)

        hub = getattr(self._orch, "hub", None)
        if hub:
            try:
                await hub.task_cancelled(task_id, row.get("agent_name", ""))
            except Exception:
                pass

        return {"task_id": task_id, "status": "cancelled", "cancelled": cancelled}
