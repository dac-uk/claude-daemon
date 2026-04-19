"""Native task API — submit, cancel, look up tasks.

Thin layer over Orchestrator.spawn_task() + ConversationStore.get_task() that
adds external-facing semantics: pre-created DB rows (so callers get a task_id
before the worker starts), uniform response shape, optional metadata.

Designed to be called from HTTP handlers and from the Paperclip compat shim.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_daemon.agents.orchestrator import Orchestrator
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.memory.store import ConversationStore
    from claude_daemon.orchestration.approvals import ApprovalsStore
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
    source: str = "api"  # api | chat | spawn | heartbeat | factory
    # When True, the task is created as ``pending_approval`` regardless
    # of budget — used by the Software Factory so plan artefacts are
    # always human-gated.
    require_approval: bool = False
    # Reason to record on the approvals row when require_approval=True.
    approval_reason: str = "manual approval requested"


@dataclass
class _ForcedApprovalDecision:
    """Stand-in for an EnforcementDecision when callers force an
    approval gate without a budget check.

    Only carries the fields ``_handle_approval_required`` reads
    (``reason`` and ``threshold_usd``).
    """

    reason: str = "manual approval requested"
    threshold_usd: float | None = None


@dataclass
class TaskSubmissionResult:
    """Response shape for TaskAPI.submit_task()."""

    task_id: str
    status: str  # pending | running | rejected | pending_approval | error
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
        approvals_store: ApprovalsStore | None = None,
    ) -> None:
        self._orch = orchestrator
        self._registry = registry
        self._store = store
        self._budget_store = budget_store
        self._approvals_store = approvals_store

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

        # Explicit approval gate (e.g. Software Factory plan artefacts).
        # Short-circuits budget enforcement so approval is always required
        # regardless of estimated spend.
        if req.require_approval:
            forced = _ForcedApprovalDecision(reason=req.approval_reason)
            return self._handle_approval_required(req, agent_name, forced)

        # Budget enforcement (Phase 2)
        reservations: list[tuple[int, float]] = []
        if self._budget_store is not None:
            from claude_daemon.orchestration.enforcement import enforce_budget
            decision = enforce_budget(
                self._budget_store,
                agent_name=agent_name,
                user_id=req.user_id,
                task_type=req.task_type,
            )
            if decision.outcome == "rejected":
                return TaskSubmissionResult(
                    task_id="", status="rejected",
                    agent=agent_name, error=decision.reason,
                )
            if decision.outcome == "approval_required":
                return self._handle_approval_required(
                    req, agent_name, decision,
                )
            reservations = decision.reservations

        # Generate task_id up-front so caller gets it synchronously
        task_id = str(uuid.uuid4())[:12]

        # Stash reservations in metadata so completion/cancel can drain them
        meta = dict(req.metadata) if req.metadata else {}
        if reservations:
            meta["_budget_reservations"] = [[bid, amt] for bid, amt in reservations]
        metadata_json = json.dumps(meta) if meta else None

        try:
            self._store.create_task(
                task_id, agent_name, req.prompt[:2000],
                task_type=req.task_type, platform=req.platform, user_id=req.user_id,
                metadata=metadata_json, goal_id=req.goal_id,
                source=req.source,
            )
        except Exception:
            log.exception("Could not persist task %s to DB", task_id)
            if reservations and self._budget_store:
                self._budget_store.release_reservations(reservations)
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
            if reservations and self._budget_store:
                self._budget_store.release_reservations(reservations)
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

    def _handle_approval_required(
        self,
        req: TaskSubmission,
        agent_name: str,
        decision,
    ) -> TaskSubmissionResult:
        """Create task_queue row + approvals row for tasks needing human review.

        The task is persisted directly as ``pending_approval`` (single INSERT,
        no transient ``pending`` window).  An approvals row is written, then
        the hub is notified with the correct ``(approval_id, task_id, reason)``
        signature.
        """
        task_id = str(uuid.uuid4())[:12]

        meta = dict(req.metadata) if req.metadata else {}
        metadata_json = json.dumps(meta) if meta else None

        try:
            self._store.create_task(
                task_id, agent_name, req.prompt[:2000],
                task_type=req.task_type, platform=req.platform,
                user_id=req.user_id, metadata=metadata_json,
                goal_id=req.goal_id, initial_status="pending_approval",
                source=req.source,
            )
        except Exception:
            log.exception("Could not persist approval task %s", task_id)
            return TaskSubmissionResult(
                task_id="", status="error", agent=agent_name,
                error="DB persistence failed",
            )

        approval_id: int | None = None
        if self._approvals_store:
            try:
                approval_id = self._approvals_store.create(
                    task_id=task_id,
                    reason=decision.reason,
                    threshold_usd=decision.threshold_usd,
                )
            except Exception:
                log.exception("Could not create approval for task %s", task_id)

        hub = getattr(self._orch, "hub", None)
        if hub and approval_id is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    hub.approval_requested(
                        approval_id, task_id, decision.reason,
                    ),
                )
            except RuntimeError:
                # Not running under an event loop (sync caller / test).
                log.debug(
                    "approval_requested broadcast skipped — no running loop",
                )
            except Exception:
                log.exception("Failed to broadcast approval_requested")

        return TaskSubmissionResult(
            task_id=task_id, status="pending_approval", agent=agent_name,
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

        Releases any budget reservations, cancels the asyncio future if alive,
        updates DB row, and broadcasts.
        Returns {"task_id", "status", "cancelled": bool}.
        """
        row = self._store.get_task(task_id)
        if row is None:
            return {"task_id": task_id, "status": "unknown", "cancelled": False}

        # Release budget reservations stored in metadata
        if self._budget_store and row.get("metadata"):
            try:
                meta = json.loads(row["metadata"])
                reservations = meta.get("_budget_reservations", [])
                if reservations:
                    typed = [(int(bid), float(amt)) for bid, amt in reservations]
                    self._budget_store.release_reservations(typed)
            except (ValueError, TypeError) as exc:
                log.warning(
                    "cancel_task %s: metadata JSON corrupt — reservations "
                    "may leak (%s)", task_id, exc,
                )
            except Exception:
                log.exception(
                    "cancel_task %s: failed to release reservations", task_id,
                )

        # Resolve linked approval row if the task was waiting on one.
        if self._approvals_store:
            try:
                approval = self._approvals_store.get_by_task(task_id)
                if approval and approval["status"] == "pending":
                    self._approvals_store.reject(
                        approval["id"], approver="cancel",
                    )
            except Exception:
                log.exception(
                    "cancel_task %s: failed to resolve approval row", task_id,
                )

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
