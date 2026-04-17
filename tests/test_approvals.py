"""Tests for the approval queue — ApprovalsStore + approve/reject workflow."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_daemon.memory.store import ConversationStore
from claude_daemon.orchestration.approvals import ApprovalsStore


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def appr(store: ConversationStore) -> ApprovalsStore:
    return ApprovalsStore(store)


# ── CRUD ──────────────────────────────────────────────────────

class TestApprovalsCRUD:
    def test_create_and_get(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "some prompt")
        aid = appr.create(task_id="t1", reason="over threshold", threshold_usd=0.05)
        row = appr.get(aid)
        assert row is not None
        assert row["task_id"] == "t1"
        assert row["status"] == "pending"
        assert row["reason"] == "over threshold"
        assert row["threshold_usd"] == 0.05
        assert row["resolved_at"] is None

    def test_list_pending(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1")
        store.create_task("t2", "albert", "p2")
        appr.create(task_id="t1")
        appr.create(task_id="t2")
        pending = appr.list_pending()
        assert len(pending) == 2

    def test_list_all(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1")
        store.create_task("t2", "albert", "p2")
        a1 = appr.create(task_id="t1")
        appr.create(task_id="t2")
        appr.approve(a1)
        all_items = appr.list_all()
        assert len(all_items) == 2

    def test_get_by_task(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1")
        appr.create(task_id="t1", reason="test")
        row = appr.get_by_task("t1")
        assert row is not None
        assert row["reason"] == "test"

    def test_get_by_task_nonexistent(self, appr: ApprovalsStore):
        assert appr.get_by_task("nope") is None

    def test_get_nonexistent(self, appr: ApprovalsStore):
        assert appr.get(999) is None


# ── Approve ──────────────────────────────────────────────────

class TestApproveFlow:
    def test_approve_sets_status(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        ok = appr.approve(aid, approver="bob")
        assert ok is True
        row = appr.get(aid)
        assert row["status"] == "approved"
        assert row["approver_user"] == "bob"
        assert row["resolved_at"] is not None

    def test_approve_updates_task_to_pending(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        appr.approve(aid)
        task = store.get_task("t1")
        assert task["status"] == "pending"

    def test_approve_already_resolved_returns_false(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        appr.approve(aid)
        assert appr.approve(aid) is False

    def test_approve_nonexistent_returns_false(self, appr: ApprovalsStore):
        assert appr.approve(999) is False


# ── Reject ───────────────────────────────────────────────────

class TestRejectFlow:
    def test_reject_sets_status(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        ok = appr.reject(aid, approver="alice")
        assert ok is True
        row = appr.get(aid)
        assert row["status"] == "rejected"
        assert row["approver_user"] == "alice"
        assert row["resolved_at"] is not None

    def test_reject_cancels_task(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        appr.reject(aid)
        task = store.get_task("t1")
        assert task["status"] == "cancelled"

    def test_reject_already_resolved_returns_false(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        appr.reject(aid)
        assert appr.reject(aid) is False

    def test_reject_nonexistent_returns_false(self, appr: ApprovalsStore):
        assert appr.reject(999) is False


# ── Edge cases ───────────────────────────────────────────────

class TestApprovalEdgeCases:
    def test_approve_then_reject_fails(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        appr.approve(aid)
        assert appr.reject(aid) is False

    def test_reject_then_approve_fails(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        aid = appr.create(task_id="t1")
        appr.reject(aid)
        assert appr.approve(aid) is False

    def test_list_pending_excludes_resolved(self, appr: ApprovalsStore, store):
        store.create_task("t1", "albert", "p1", initial_status="pending_approval")
        store.create_task("t2", "albert", "p2", initial_status="pending_approval")
        a1 = appr.create(task_id="t1")
        appr.create(task_id="t2")
        appr.approve(a1)
        pending = appr.list_pending()
        assert len(pending) == 1
        assert pending[0]["task_id"] == "t2"
