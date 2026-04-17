"""Phase 6 regression tests — re-enforce-on-approve, atomic transitions, orphan sweep."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_daemon.memory.store import ConversationStore
from claude_daemon.orchestration.approvals import ApprovalsStore
from claude_daemon.orchestration.budgets import BudgetStore
from claude_daemon.orchestration.enforcement import enforce_budget
from claude_daemon.orchestration.goals import GoalsStore
from claude_daemon.orchestration import TaskAPI, TaskSubmission


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    s = ConversationStore(tmp_path / "p6.db")
    yield s
    s.close()


@pytest.fixture
def bs(store: ConversationStore) -> BudgetStore:
    return BudgetStore(store)


@pytest.fixture
def appr(store: ConversationStore) -> ApprovalsStore:
    return ApprovalsStore(store)


@pytest.fixture
def gs(store: ConversationStore) -> GoalsStore:
    return GoalsStore(store)


def _make_api(store, bs=None, appr=None, hub=None):
    agents = {"albert": MagicMock(name="albert")}
    agents["albert"].name = "albert"
    registry = MagicMock()
    registry.get.side_effect = lambda n: agents.get(n)
    registry.get_orchestrator.return_value = agents["albert"]
    registry.list_agents.return_value = list(agents.values())
    orch = MagicMock()
    orch._spawned_tasks = {}
    orch.spawn_task = MagicMock(return_value=MagicMock(task_id="x"))
    orch.hub = hub
    return TaskAPI(
        orchestrator=orch, registry=registry, store=store,
        budget_store=bs, approvals_store=appr,
    ), orch


# ── C1: approved task charges actual cost against budget ─────────


class TestApproveChargesBudget:
    def test_approve_path_reserves_and_applies_actual_spend(self, store, bs, appr):
        bid = bs.create(scope="global", limit_usd=10.0, period="daily",
                        approval_threshold_usd=0.005)
        api, _ = _make_api(store, bs, appr)

        # Submit — lands in pending_approval; reservations NOT held.
        result = api.submit_task(TaskSubmission(prompt="big", agent="albert"))
        assert result.status == "pending_approval"
        assert bs.get(bid)["current_spend"] == pytest.approx(0.0)

        # Simulate the approve handler: re-enforce (skip threshold) then persist
        # reservations into metadata.
        decision = enforce_budget(
            bs, agent_name="albert", skip_approval_threshold=True,
        )
        assert decision.outcome == "allowed"
        assert len(decision.reservations) == 1
        store.update_task_metadata(
            result.task_id,
            {"_budget_reservations": [[b, a] for b, a in decision.reservations]},
        )
        assert bs.get(bid)["current_spend"] == pytest.approx(0.01)

        # Task completes at actual_cost=$0.15 — apply_actual_spend reconciles.
        bs.apply_actual_spend(decision.reservations, actual_cost=0.15)
        assert bs.get(bid)["current_spend"] == pytest.approx(0.15)


class TestApproveWithDrainedBudget:
    def test_reenforce_rejects_when_drained(self, store, bs, appr):
        bs.create(scope="global", limit_usd=1.0, period="daily",
                  approval_threshold_usd=0.005)
        api, _ = _make_api(store, bs, appr)
        # Submit one big task that will need approval.
        result = api.submit_task(TaskSubmission(prompt="a", agent="albert"))
        assert result.status == "pending_approval"

        # Drain the budget from another path (leave < $0.01 headroom).
        bid = bs.list_all()[0]["id"]
        bs.check_and_reserve(bid, 1.0)

        # Re-enforce (as approve handler would, skip_approval_threshold=True)
        decision = enforce_budget(
            bs, agent_name="albert", skip_approval_threshold=True,
        )
        assert decision.outcome == "rejected"


# ── C2: approve cannot resurrect a cancelled task ────────────────


class TestApproveCancelledTask:
    def test_cancelled_then_approve_returns_false(self, store, appr):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        store.update_task_status("t1", "cancelled")

        ok = appr.approve(aid, approver="a")
        assert ok is False
        row = appr.get(aid)
        assert row["status"] == "stale"
        task = store.get_task("t1")
        assert task["status"] == "cancelled"


# ── H1/H2: hub signature + loop handling ─────────────────────────


class TestHubBroadcast:
    @pytest.mark.asyncio
    async def test_hub_called_with_approval_id_task_id_reason(self, store, bs, appr):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=0.005)
        hub = MagicMock()
        hub.approval_requested = MagicMock(
            side_effect=lambda *a, **kw: asyncio.sleep(0),
        )
        api, _ = _make_api(store, bs, appr, hub=hub)
        result = api.submit_task(TaskSubmission(prompt="x", agent="albert"))
        # Give the scheduled coroutine a tick.
        await asyncio.sleep(0)
        hub.approval_requested.assert_called_once()
        args = hub.approval_requested.call_args.args
        # (approval_id: int, task_id: str, reason: str)
        assert isinstance(args[0], int)
        assert args[1] == result.task_id
        assert "threshold" in args[2].lower()

    def test_no_running_loop_does_not_raise(self, store, bs, appr):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=0.005)
        hub = MagicMock()
        api, _ = _make_api(store, bs, appr, hub=hub)
        # Sync call — no event loop running. Should not raise.
        result = api.submit_task(TaskSubmission(prompt="x", agent="albert"))
        assert result.status == "pending_approval"


# ── H3: pending_approval inserted directly (no transient pending) ─


class TestInitialStatus:
    def test_create_task_with_pending_approval(self, store):
        store.create_task(
            "t9", "albert", "p", initial_status="pending_approval",
        )
        row = store.get_task("t9")
        assert row["status"] == "pending_approval"


# ── H4: rejection beats approval_required ────────────────────────


class TestRejectionBeatsApproval:
    def test_exhausted_and_threshold_returns_rejected(self, bs):
        # Budget already drained AND has a threshold that would trigger.
        bid = bs.create(scope="global", limit_usd=1.0, period="daily",
                        approval_threshold_usd=0.005)
        bs.check_and_reserve(bid, 1.0)  # fully drained
        decision = enforce_budget(bs, agent_name="albert")
        assert decision.outcome == "rejected"


# ── H5: cancel resolves approval row ─────────────────────────────


class TestCancelResolvesApproval:
    @pytest.mark.asyncio
    async def test_cancel_rejects_pending_approval(self, store, bs, appr):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=0.005)
        api, _ = _make_api(store, bs, appr)
        result = api.submit_task(TaskSubmission(prompt="x", agent="albert"))
        approval_row = appr.get_by_task(result.task_id)
        assert approval_row["status"] == "pending"

        await api.cancel_task(result.task_id)

        approval_row = appr.get_by_task(result.task_id)
        assert approval_row["status"] == "rejected"


# ── H7: startup orphan sweep ─────────────────────────────────────


class TestOrphanSweep:
    def test_sweep_marks_running_and_pending_failed(self, store):
        store.create_task("running1", "a", "p")
        store.update_task_status("running1", "running")
        store.create_task("pending1", "a", "p")
        store.create_task("keep1", "a", "p", initial_status="pending_approval")

        orphans = store.sweep_orphan_tasks(live_ids=set())
        assert {o["id"] for o in orphans} == {"running1", "pending1"}
        assert store.get_task("running1")["status"] == "failed"
        assert store.get_task("pending1")["status"] == "failed"
        # pending_approval must not be swept.
        assert store.get_task("keep1")["status"] == "pending_approval"

    def test_sweep_preserves_live_ids(self, store):
        store.create_task("alive", "a", "p")
        store.update_task_status("alive", "running")
        orphans = store.sweep_orphan_tasks(live_ids={"alive"})
        assert orphans == []
        assert store.get_task("alive")["status"] == "running"


# ── M1: corrupt metadata on cancel is logged ─────────────────────


class TestCancelLogsBadMetadata:
    @pytest.mark.asyncio
    async def test_corrupt_metadata_warning(self, store, bs, caplog):
        bid = bs.create(scope="global", limit_usd=10.0, period="daily")
        api, _ = _make_api(store, bs)
        store.create_task("c1", "albert", "p", metadata="{not json")
        with caplog.at_level(logging.WARNING):
            await api.cancel_task("c1")
        assert any(
            "metadata JSON corrupt" in r.message for r in caplog.records
        )


# ── M3: release_reservations on deleted budget logs warning ──────


class TestReleaseMissingBudgetLogs:
    def test_deleted_budget_logs_warning(self, bs, caplog):
        bid = bs.create(scope="global", limit_usd=10.0, period="daily")
        bs.check_and_reserve(bid, 0.5)
        bs.delete(bid)
        with caplog.at_level(logging.WARNING):
            bs.release_reservations([(bid, 0.5)])
        assert any(
            "missing" in r.message and str(bid) in r.message
            for r in caplog.records
        )
