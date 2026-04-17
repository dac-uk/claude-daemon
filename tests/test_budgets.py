"""Tests for budget caps — BudgetStore CRUD, enforcement, atomic reservation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_daemon.memory.store import ConversationStore
from claude_daemon.orchestration.budgets import BudgetStore
from claude_daemon.orchestration.enforcement import (
    EnforcementDecision,
    enforce_budget,
)
from claude_daemon.orchestration import TaskAPI, TaskSubmission


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def bs(store: ConversationStore) -> BudgetStore:
    return BudgetStore(store)


# ── CRUD ──────────────────────────────────────────────────────

class TestBudgetCRUD:
    def test_create_and_get(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=10.0, period="daily")
        row = bs.get(bid)
        assert row is not None
        assert row["scope"] == "global"
        assert row["limit_usd"] == 10.0
        assert row["period"] == "daily"
        assert row["current_spend"] == 0.0
        assert row["enabled"] == 1
        assert row["reset_at"] is not None

    def test_create_agent_scope(self, bs: BudgetStore):
        bid = bs.create(scope="agent", limit_usd=5.0, period="monthly",
                        scope_value="albert")
        row = bs.get(bid)
        assert row["scope_value"] == "albert"

    def test_create_lifetime_has_no_reset(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=100.0, period="lifetime")
        row = bs.get(bid)
        assert row["reset_at"] is None

    def test_create_invalid_scope_raises(self, bs: BudgetStore):
        with pytest.raises(ValueError, match="Invalid scope"):
            bs.create(scope="bogus", limit_usd=1.0, period="daily")

    def test_create_invalid_period_raises(self, bs: BudgetStore):
        with pytest.raises(ValueError, match="Invalid period"):
            bs.create(scope="global", limit_usd=1.0, period="hourly")

    def test_create_zero_limit_raises(self, bs: BudgetStore):
        with pytest.raises(ValueError, match="positive"):
            bs.create(scope="global", limit_usd=0, period="daily")

    def test_list_all(self, bs: BudgetStore):
        bs.create(scope="global", limit_usd=10.0, period="daily")
        bs.create(scope="agent", limit_usd=5.0, period="weekly", scope_value="luna")
        rows = bs.list_all()
        assert len(rows) == 2

    def test_list_enabled_only(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=10.0, period="daily")
        bs.create(scope="agent", limit_usd=5.0, period="weekly", scope_value="luna")
        bs.update(bid, enabled=False)
        rows = bs.list_all(enabled_only=True)
        assert len(rows) == 1
        assert rows[0]["scope_value"] == "luna"

    def test_update_limit(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=10.0, period="daily")
        bs.update(bid, limit_usd=20.0)
        assert bs.get(bid)["limit_usd"] == 20.0

    def test_update_nonexistent_returns_false(self, bs: BudgetStore):
        assert bs.update(999, limit_usd=5.0) is False

    def test_delete(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=10.0, period="daily")
        assert bs.delete(bid) is True
        assert bs.get(bid) is None

    def test_delete_nonexistent_returns_false(self, bs: BudgetStore):
        assert bs.delete(999) is False

    def test_get_nonexistent(self, bs: BudgetStore):
        assert bs.get(999) is None


# ── Applicable budgets ────────────────────────────────────────

class TestGetApplicable:
    def test_global_applies_to_everything(self, bs: BudgetStore):
        bs.create(scope="global", limit_usd=10.0, period="daily")
        applicable = bs.get_applicable(agent_name="albert")
        assert len(applicable) == 1

    def test_agent_scope_filters(self, bs: BudgetStore):
        bs.create(scope="agent", limit_usd=5.0, period="daily", scope_value="albert")
        assert len(bs.get_applicable(agent_name="albert")) == 1
        assert len(bs.get_applicable(agent_name="luna")) == 0

    def test_user_scope_filters(self, bs: BudgetStore):
        bs.create(scope="user", limit_usd=5.0, period="daily", scope_value="bob")
        assert len(bs.get_applicable(user_id="bob")) == 1
        assert len(bs.get_applicable(user_id="alice")) == 0

    def test_task_type_scope_filters(self, bs: BudgetStore):
        bs.create(scope="task_type", limit_usd=5.0, period="daily",
                  scope_value="code_review")
        assert len(bs.get_applicable(task_type="code_review")) == 1
        assert len(bs.get_applicable(task_type="chat")) == 0

    def test_disabled_excluded(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=10.0, period="daily")
        bs.update(bid, enabled=False)
        assert len(bs.get_applicable()) == 0


# ── Atomic reservation ────────────────────────────────────────

class TestCheckAndReserve:
    def test_reserve_success(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=1.0, period="daily")
        assert bs.check_and_reserve(bid, 0.50) is True
        assert bs.get(bid)["current_spend"] == pytest.approx(0.50)

    def test_reserve_exactly_at_limit(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=1.0, period="daily")
        assert bs.check_and_reserve(bid, 1.0) is True
        assert bs.get(bid)["current_spend"] == pytest.approx(1.0)

    def test_reserve_over_limit_fails(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=1.0, period="daily")
        bs.check_and_reserve(bid, 0.90)
        assert bs.check_and_reserve(bid, 0.20) is False
        assert bs.get(bid)["current_spend"] == pytest.approx(0.90)

    def test_reserve_disabled_fails(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=10.0, period="daily")
        bs.update(bid, enabled=False)
        assert bs.check_and_reserve(bid, 0.01) is False

    def test_release_reservation(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=1.0, period="daily")
        bs.check_and_reserve(bid, 0.50)
        bs.release_reservation(bid, 0.50)
        assert bs.get(bid)["current_spend"] == pytest.approx(0.0)

    def test_release_floors_at_zero(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=1.0, period="daily")
        bs.release_reservation(bid, 10.0)
        assert bs.get(bid)["current_spend"] == 0.0


# ── Spend recording ──────────────────────────────────────────

class TestRecordSpend:
    def test_records_to_applicable(self, bs: BudgetStore):
        bs.create(scope="global", limit_usd=10.0, period="daily")
        bs.create(scope="agent", limit_usd=5.0, period="daily", scope_value="albert")
        updated = bs.record_spend(agent_name="albert", actual_cost=0.25)
        assert len(updated) == 2
        for b in updated:
            assert b["current_spend"] == pytest.approx(0.25)

    def test_zero_cost_skipped(self, bs: BudgetStore):
        bs.create(scope="global", limit_usd=10.0, period="daily")
        updated = bs.record_spend(actual_cost=0.0)
        assert updated == []

    def test_negative_cost_skipped(self, bs: BudgetStore):
        bs.create(scope="global", limit_usd=10.0, period="daily")
        updated = bs.record_spend(actual_cost=-1.0)
        assert updated == []


# ── Enforcement ──────────────────────────────────────────────

class TestEnforcement:
    def test_no_budgets_allows(self, bs: BudgetStore):
        decision = enforce_budget(bs, agent_name="albert")
        assert decision.allowed
        assert decision.outcome == "allowed"

    def test_budget_with_headroom_allows(self, bs: BudgetStore):
        bs.create(scope="global", limit_usd=10.0, period="daily")
        decision = enforce_budget(bs, agent_name="albert")
        assert decision.allowed
        assert len(decision.reservations) == 1

    def test_exhausted_budget_rejects(self, bs: BudgetStore):
        bid = bs.create(scope="global", limit_usd=0.01, period="daily")
        bs.check_and_reserve(bid, 0.01)
        decision = enforce_budget(bs, agent_name="albert")
        assert not decision.allowed
        assert decision.outcome == "rejected"
        assert len(decision.blocked_by) == 1

    def test_partial_reservation_rolled_back(self, bs: BudgetStore):
        bid1 = bs.create(scope="global", limit_usd=10.0, period="daily")
        bid2 = bs.create(scope="agent", limit_usd=0.005, period="daily",
                         scope_value="albert")
        bs.check_and_reserve(bid2, 0.005)
        decision = enforce_budget(bs, agent_name="albert")
        assert not decision.allowed
        # Global budget reservation should have been rolled back
        global_b = bs.get(bid1)
        assert global_b["current_spend"] == pytest.approx(0.0)

    def test_approval_threshold_triggers(self, bs: BudgetStore):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=0.005)
        decision = enforce_budget(bs, agent_name="albert", estimated_cost=0.01)
        assert not decision.allowed
        assert decision.outcome == "approval_required"

    def test_approval_threshold_under_allows(self, bs: BudgetStore):
        bs.create(scope="global", limit_usd=10.0, period="daily",
                  approval_threshold_usd=1.0)
        decision = enforce_budget(bs, agent_name="albert", estimated_cost=0.01)
        assert decision.allowed


# ── TaskAPI integration ──────────────────────────────────────

class TestTaskAPIBudgetIntegration:
    def _make_api(self, store, bs):
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
        return TaskAPI(orchestrator=orch, registry=registry, store=store,
                       budget_store=bs)

    def test_submit_allowed_when_budget_has_headroom(self, store, bs):
        bs.create(scope="global", limit_usd=10.0, period="daily")
        api = self._make_api(store, bs)
        result = api.submit_task(TaskSubmission(prompt="test", agent="albert"))
        assert result.status == "pending"

    def test_submit_rejected_when_budget_exhausted(self, store, bs):
        bid = bs.create(scope="global", limit_usd=0.005, period="daily")
        bs.check_and_reserve(bid, 0.005)
        api = self._make_api(store, bs)
        result = api.submit_task(TaskSubmission(prompt="test", agent="albert"))
        assert result.status == "rejected"
        assert "exceeded" in (result.error or "").lower()

    def test_submit_without_budget_store_proceeds(self, store):
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
        api = TaskAPI(orchestrator=orch, registry=registry, store=store,
                      budget_store=None)
        result = api.submit_task(TaskSubmission(prompt="test", agent="albert"))
        assert result.status == "pending"
