"""Tests for goal tracking — GoalsStore CRUD + progress aggregation."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_daemon.memory.store import ConversationStore
from claude_daemon.orchestration.goals import GoalsStore


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def gs(store: ConversationStore) -> GoalsStore:
    return GoalsStore(store)


# ── CRUD ──────────────────────────────────────────────────────

class TestGoalsCRUD:
    def test_create_and_get(self, gs: GoalsStore):
        gid = gs.create(title="Ship Phase 3")
        row = gs.get(gid)
        assert row is not None
        assert row["title"] == "Ship Phase 3"
        assert row["status"] == "active"
        assert row["completed_at"] is None

    def test_create_with_all_fields(self, gs: GoalsStore):
        gid = gs.create(
            title="Deploy new feature",
            description="Full rollout of budget caps",
            owner_agent="albert",
            target_date="2026-05-01",
        )
        row = gs.get(gid)
        assert row["description"] == "Full rollout of budget caps"
        assert row["owner_agent"] == "albert"
        assert row["target_date"] == "2026-05-01"

    def test_create_empty_title_raises(self, gs: GoalsStore):
        with pytest.raises(ValueError, match="empty"):
            gs.create(title="")

    def test_create_whitespace_title_raises(self, gs: GoalsStore):
        with pytest.raises(ValueError, match="empty"):
            gs.create(title="   ")

    def test_create_child_goal(self, gs: GoalsStore):
        parent = gs.create(title="Parent goal")
        child = gs.create(title="Sub-task", parent_goal_id=parent)
        row = gs.get(child)
        assert row["parent_goal_id"] == parent

    def test_list_all(self, gs: GoalsStore):
        gs.create(title="Goal A")
        gs.create(title="Goal B")
        assert len(gs.list_all()) == 2

    def test_list_filtered_by_status(self, gs: GoalsStore):
        gid = gs.create(title="Goal A")
        gs.create(title="Goal B")
        gs.update(gid, status="completed")
        active = gs.list_all(status="active")
        assert len(active) == 1
        assert active[0]["title"] == "Goal B"

    def test_list_filtered_by_owner(self, gs: GoalsStore):
        gs.create(title="Albert's goal", owner_agent="albert")
        gs.create(title="Luna's goal", owner_agent="luna")
        rows = gs.list_all(owner_agent="albert")
        assert len(rows) == 1
        assert rows[0]["owner_agent"] == "albert"

    def test_update_title(self, gs: GoalsStore):
        gid = gs.create(title="Old")
        gs.update(gid, title="New")
        assert gs.get(gid)["title"] == "New"

    def test_update_status_to_completed_sets_timestamp(self, gs: GoalsStore):
        gid = gs.create(title="Goal")
        gs.update(gid, status="completed")
        row = gs.get(gid)
        assert row["status"] == "completed"
        assert row["completed_at"] is not None

    def test_update_invalid_status_raises(self, gs: GoalsStore):
        gid = gs.create(title="Goal")
        with pytest.raises(ValueError, match="Invalid status"):
            gs.update(gid, status="bogus")

    def test_update_nonexistent_returns_false(self, gs: GoalsStore):
        assert gs.update(999, title="nope") is False

    def test_delete(self, gs: GoalsStore):
        gid = gs.create(title="Gone")
        assert gs.delete(gid) is True
        assert gs.get(gid) is None

    def test_delete_nonexistent_returns_false(self, gs: GoalsStore):
        assert gs.delete(999) is False

    def test_get_nonexistent(self, gs: GoalsStore):
        assert gs.get(999) is None


# ── Progress aggregation ──────────────────────────────────────

class TestGoalProgress:
    def test_no_tasks_returns_zero(self, gs: GoalsStore, store):
        gid = gs.create(title="Empty goal")
        progress = gs.compute_progress(gid)
        assert progress["total"] == 0
        assert progress["pct"] == 0

    def test_progress_from_linked_tasks(self, gs: GoalsStore, store):
        gid = gs.create(title="Tracked goal")
        store.create_task("t1", "albert", "task1", goal_id=gid)
        store.create_task("t2", "albert", "task2", goal_id=gid)
        store.create_task("t3", "albert", "task3", goal_id=gid)
        store.update_task_status("t1", "completed", cost_usd=0.05)
        store.update_task_status("t2", "completed", cost_usd=0.03)

        progress = gs.compute_progress(gid)
        assert progress["total"] == 3
        assert progress["completed"] == 2
        assert progress["pending"] == 1
        assert progress["pct"] == pytest.approx(66.7)
        assert progress["total_cost"] == pytest.approx(0.08)

    def test_progress_ignores_other_goals(self, gs: GoalsStore, store):
        gid1 = gs.create(title="Goal 1")
        gid2 = gs.create(title="Goal 2")
        store.create_task("t1", "albert", "task1", goal_id=gid1)
        store.create_task("t2", "albert", "task2", goal_id=gid2)
        progress = gs.compute_progress(gid1)
        assert progress["total"] == 1

    def test_all_completed_100_pct(self, gs: GoalsStore, store):
        gid = gs.create(title="Done goal")
        store.create_task("t1", "albert", "task1", goal_id=gid)
        store.update_task_status("t1", "completed")
        progress = gs.compute_progress(gid)
        assert progress["pct"] == 100.0


# ── Children ──────────────────────────────────────────────────

class TestGoalChildren:
    def test_get_children(self, gs: GoalsStore):
        parent = gs.create(title="Parent")
        c1 = gs.create(title="Child 1", parent_goal_id=parent)
        c2 = gs.create(title="Child 2", parent_goal_id=parent)
        gs.create(title="Unrelated")
        children = gs.get_children(parent)
        assert len(children) == 2
        assert {c["id"] for c in children} == {c1, c2}

    def test_no_children(self, gs: GoalsStore):
        gid = gs.create(title="Leaf")
        assert gs.get_children(gid) == []
