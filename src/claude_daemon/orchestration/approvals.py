"""Approval queue — gate high-cost tasks behind human review.

When budget enforcement returns ``approval_required`` (because estimated cost
exceeds ``budgets.approval_threshold_usd``), the task is created with status
``pending_approval`` and an ``approvals`` row is inserted.

Callers approve or reject via ``POST /api/v1/approvals/{id}/approve`` or
``/reject``.  On approval the task is dispatched to the orchestrator;
on rejection the task is cancelled.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)


class ApprovalsStore:
    """CRUD on the ``approvals`` table + approve/reject workflow."""

    def __init__(self, store: ConversationStore) -> None:
        self._db = store._db
        self._store = store

    # ── Create ────────────────────────────────────────────────

    def create(
        self,
        task_id: str,
        reason: str = "",
        threshold_usd: float | None = None,
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO approvals (task_id, reason, threshold_usd) VALUES (?, ?, ?)",
            (task_id, reason, threshold_usd),
        )
        self._db.commit()
        return cur.lastrowid

    # ── Read ──────────────────────────────────────────────────

    def get(self, approval_id: int) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_pending(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at",
        ).fetchall()
        return [dict(r) for r in rows]

    def list_all(self, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_by_task(self, task_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    # ── Resolve ───────────────────────────────────────────────

    def approve(self, approval_id: int, approver: str = "local") -> bool:
        """Mark approval as approved, update linked task to 'pending'.

        Uses atomic UPDATE … WHERE status='pending' to prevent TOCTOU races.
        """
        now = datetime.now(timezone.utc).isoformat()
        cur = self._db.execute(
            "UPDATE approvals SET status = 'approved', approver_user = ?, "
            "resolved_at = ? WHERE id = ? AND status = 'pending'",
            (approver, now, approval_id),
        )
        if cur.rowcount == 0:
            return False
        row = self.get(approval_id)
        if row:
            self._db.execute(
                "UPDATE task_queue SET status = 'pending' WHERE id = ?",
                (row["task_id"],),
            )
        self._db.commit()
        return True

    def reject(self, approval_id: int, approver: str = "local") -> bool:
        """Mark approval as rejected, cancel linked task.

        Uses atomic UPDATE … WHERE status='pending' to prevent TOCTOU races.
        """
        now = datetime.now(timezone.utc).isoformat()
        cur = self._db.execute(
            "UPDATE approvals SET status = 'rejected', approver_user = ?, "
            "resolved_at = ? WHERE id = ? AND status = 'pending'",
            (approver, now, approval_id),
        )
        if cur.rowcount == 0:
            return False
        row = self.get(approval_id)
        if row:
            self._db.execute(
                "UPDATE task_queue SET status = 'cancelled' WHERE id = ?",
                (row["task_id"],),
            )
        self._db.commit()
        return True
