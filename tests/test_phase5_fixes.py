"""Phase 5 regression tests — approval workflow, reservation lifecycle, atomicity."""

from __future__ import annotations

import json
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
    s = ConversationStore(tmp_path / "test.db")
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


def _make_api(store, bs=None, appr=None):
    agents = {"albert": MagicMock(name="albert")}
    agents["albert"].name = "albert"
    registry = MagicMock()
    registry.get.side_effect = lambda n: agents.get(n)
    registry.get_orchestrator.return_value = agents["albert"]
    registry.list_agents.return_value = list(agents.values())
    orch = MagicMock()
    orch._spawned_tasks = {}
    orch.spawn_task = MagicMock(return_value=MagicMock(task_id="x"))
    orch.hub = None
    return TaskAPI(
        orchestrator=orch, registry=registry, store=store,
        budget_store=bs, approvals_store=appr,
    ), orch


# ── Bug #1: approval_required creates task_queue + approvals rows ──


class TestApprovalWorkflowE2E:
    def test_submit_approval_required_creates_task_row(self, store, bs, appr):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=0.005)
        api, _ = _make_api(store, bs, appr)
        result = api.submit_task(TaskSubmission(prompt="big task", agent="albert"))
        assert result.status == "pending_approval"
        assert result.task_id != ""
        row = store.get_task(result.task_id)
        assert row is not None
        assert row["status"] == "pending_approval"
        assert row["agent_name"] == "albert"
        assert row["prompt"] == "big task"

    def test_submit_approval_required_creates_approvals_row(self, store, bs, appr):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=0.005)
        api, _ = _make_api(store, bs, appr)
        result = api.submit_task(TaskSubmission(prompt="big task", agent="albert"))
        approval = appr.get_by_task(result.task_id)
        assert approval is not None
        assert approval["status"] == "pending"
        assert "threshold" in approval["reason"].lower()

    def test_submit_approval_required_does_not_spawn(self, store, bs, appr):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=0.005)
        api, orch = _make_api(store, bs, appr)
        api.submit_task(TaskSubmission(prompt="big task", agent="albert"))
        orch.spawn_task.assert_not_called()

    def test_approve_then_spawn(self, store, bs, appr):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=0.005)
        api, _ = _make_api(store, bs, appr)
        result = api.submit_task(TaskSubmission(prompt="big task", agent="albert"))
        approval = appr.get_by_task(result.task_id)
        ok = appr.approve(approval["id"], approver="admin")
        assert ok is True
        task = store.get_task(result.task_id)
        assert task["status"] == "pending"

    def test_reject_cancels_task(self, store, bs, appr):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=0.005)
        api, _ = _make_api(store, bs, appr)
        result = api.submit_task(TaskSubmission(prompt="big task", agent="albert"))
        approval = appr.get_by_task(result.task_id)
        appr.reject(approval["id"])
        task = store.get_task(result.task_id)
        assert task["status"] == "cancelled"


# ── Bug #2: no double-counting of reservations + actual spend ──


class TestReservationLifecycle:
    def test_apply_actual_spend_replaces_reservation(self, bs):
        bid = bs.create(scope="global", limit_usd=10.0, period="daily")
        bs.check_and_reserve(bid, 0.01)
        assert bs.get(bid)["current_spend"] == pytest.approx(0.01)
        bs.apply_actual_spend([(bid, 0.01)], actual_cost=0.05)
        assert bs.get(bid)["current_spend"] == pytest.approx(0.05)

    def test_apply_actual_spend_refunds_when_cheaper(self, bs):
        bid = bs.create(scope="global", limit_usd=10.0, period="daily")
        bs.check_and_reserve(bid, 0.10)
        bs.apply_actual_spend([(bid, 0.10)], actual_cost=0.03)
        assert bs.get(bid)["current_spend"] == pytest.approx(0.03)

    def test_release_reservations_batch(self, bs):
        b1 = bs.create(scope="global", limit_usd=10.0, period="daily")
        b2 = bs.create(scope="agent", limit_usd=5.0, period="daily",
                       scope_value="albert")
        bs.check_and_reserve(b1, 0.01)
        bs.check_and_reserve(b2, 0.01)
        bs.release_reservations([(b1, 0.01), (b2, 0.01)])
        assert bs.get(b1)["current_spend"] == pytest.approx(0.0)
        assert bs.get(b2)["current_spend"] == pytest.approx(0.0)

    def test_submit_stores_reservations_in_metadata(self, store, bs):
        bs.create(scope="global", limit_usd=10.0, period="daily")
        api, _ = _make_api(store, bs)
        result = api.submit_task(TaskSubmission(prompt="test", agent="albert"))
        row = store.get_task(result.task_id)
        meta = json.loads(row["metadata"])
        assert "_budget_reservations" in meta
        assert len(meta["_budget_reservations"]) == 1


# ── Bug #3: cancel releases reservations ──


class TestCancelReleasesReservations:
    @pytest.mark.asyncio
    async def test_cancel_releases_budget(self, store, bs):
        bid = bs.create(scope="global", limit_usd=1.0, period="daily")
        api, _ = _make_api(store, bs)
        result = api.submit_task(TaskSubmission(prompt="test", agent="albert"))
        assert bs.get(bid)["current_spend"] == pytest.approx(0.01)
        await api.cancel_task(result.task_id)
        assert bs.get(bid)["current_spend"] == pytest.approx(0.0)


# ── Bug #4: spawn failure releases reservations ──


class TestSpawnFailureReleasesReservations:
    def test_spawn_exception_releases(self, store, bs):
        bid = bs.create(scope="global", limit_usd=1.0, period="daily")
        agents = {"albert": MagicMock(name="albert")}
        agents["albert"].name = "albert"
        registry = MagicMock()
        registry.get.side_effect = lambda n: agents.get(n)
        registry.get_orchestrator.return_value = agents["albert"]
        registry.list_agents.return_value = list(agents.values())
        orch = MagicMock()
        orch._spawned_tasks = {}
        orch.spawn_task.side_effect = RuntimeError("boom")
        orch.hub = None
        api = TaskAPI(
            orchestrator=orch, registry=registry, store=store, budget_store=bs,
        )
        result = api.submit_task(TaskSubmission(prompt="test", agent="albert"))
        assert result.status == "error"
        assert bs.get(bid)["current_spend"] == pytest.approx(0.0)


# ── Bug #5: get_pending_tasks includes pending_approval ──


class TestPendingApprovalVisible:
    def test_pending_approval_in_get_pending(self, store):
        store.create_task("t1", "albert", "p1")
        store.update_task_status("t1", "pending_approval")
        rows = store.get_pending_tasks()
        assert len(rows) == 1
        assert rows[0]["status"] == "pending_approval"

    def test_list_pending_api_includes_pending_approval(self, store, bs, appr):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=0.005)
        api, _ = _make_api(store, bs, appr)
        api.submit_task(TaskSubmission(prompt="big task", agent="albert"))
        rows = api.list_pending()
        assert len(rows) == 1
        assert rows[0]["status"] == "pending_approval"


# ── Bug #7: atomic approve/reject ──


class TestAtomicApproveReject:
    def test_double_approve_only_first_succeeds(self, store, appr):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        ok1 = appr.approve(aid, approver="a")
        ok2 = appr.approve(aid, approver="b")
        assert ok1 is True
        assert ok2 is False
        row = appr.get(aid)
        assert row["approver_user"] == "a"

    def test_double_reject_only_first_succeeds(self, store, appr):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        ok1 = appr.reject(aid, approver="a")
        ok2 = appr.reject(aid, approver="b")
        assert ok1 is True
        assert ok2 is False

    def test_approve_then_reject_fails(self, store, appr):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        appr.approve(aid)
        assert appr.reject(aid) is False


# ── Bug #8: delete parent goal clears children ──


class TestGoalDeleteClearsChildren:
    def test_delete_parent_nullifies_children(self, gs):
        parent = gs.create(title="Parent")
        child = gs.create(title="Child", parent_goal_id=parent)
        gs.delete(parent)
        row = gs.get(child)
        assert row is not None
        assert row["parent_goal_id"] is None


# ── Enforcement threshold_usd propagated ──


class TestEnforcementThreshold:
    def test_decision_carries_threshold(self, bs):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=0.005)
        decision = enforce_budget(bs, agent_name="albert", estimated_cost=0.01)
        assert decision.outcome == "approval_required"
        assert decision.threshold_usd == 0.005
