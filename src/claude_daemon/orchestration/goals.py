"""Goal management — CRUD and progress aggregation.

Goals provide a way to group related tasks and track collective progress.
Each goal can optionally be assigned to an owner agent, have a target date,
and nest under a parent goal via ``parent_goal_id``.

Progress is computed by counting tasks linked to the goal (via
``task_queue.goal_id``) and reporting the fraction that are completed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)

VALID_STATUSES = {"active", "completed", "cancelled", "paused"}


class GoalsStore:
    """CRUD + progress aggregation on the ``goals`` table."""

    def __init__(self, store: ConversationStore) -> None:
        self._db = store._db

    # ── CRUD ──────────────────────────────────────────────────

    def create(
        self,
        title: str,
        description: str | None = None,
        owner_agent: str | None = None,
        target_date: str | None = None,
        parent_goal_id: int | None = None,
    ) -> int:
        if not title or not title.strip():
            raise ValueError("Goal title must not be empty")
        cur = self._db.execute(
            "INSERT INTO goals "
            "(title, description, owner_agent, target_date, parent_goal_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (title.strip(), description, owner_agent, target_date, parent_goal_id),
        )
        self._db.commit()
        return cur.lastrowid

    def get(self, goal_id: int) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM goals WHERE id = ?", (goal_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_all(
        self,
        status: str | None = None,
        owner_agent: str | None = None,
    ) -> list[dict]:
        q = "SELECT * FROM goals WHERE 1=1"
        params: list = []
        if status:
            q += " AND status = ?"
            params.append(status)
        if owner_agent:
            q += " AND owner_agent = ?"
            params.append(owner_agent)
        q += " ORDER BY created_at DESC"
        return [dict(r) for r in self._db.execute(q, params).fetchall()]

    def update(
        self,
        goal_id: int,
        title: str | None = None,
        description: str | None = ...,
        owner_agent: str | None = ...,
        target_date: str | None = ...,
        status: str | None = None,
    ) -> bool:
        row = self.get(goal_id)
        if row is None:
            return False
        if status is not None and status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}")

        sets: list[str] = []
        params: list = []
        if title is not None:
            sets.append("title = ?")
            params.append(title.strip())
        if description is not ...:
            sets.append("description = ?")
            params.append(description)
        if owner_agent is not ...:
            sets.append("owner_agent = ?")
            params.append(owner_agent)
        if target_date is not ...:
            sets.append("target_date = ?")
            params.append(target_date)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            if status == "completed":
                sets.append("completed_at = ?")
                params.append(datetime.now(timezone.utc).isoformat())
        if not sets:
            return True

        params.append(goal_id)
        self._db.execute(
            f"UPDATE goals SET {', '.join(sets)} WHERE id = ?", params,
        )
        self._db.commit()
        return True

    def delete(self, goal_id: int) -> bool:
        """Delete a goal and orphan its children by clearing parent_goal_id.

        Both statements run inside a single transaction so a failure halfway
        through doesn't leave children pointing at a vanished parent.
        """
        try:
            self._db.execute(
                "UPDATE goals SET parent_goal_id = NULL WHERE parent_goal_id = ?",
                (goal_id,),
            )
            cur = self._db.execute(
                "DELETE FROM goals WHERE id = ?", (goal_id,),
            )
            self._db.commit()
            return cur.rowcount > 0
        except Exception:
            self._db.rollback()
            raise

    # ── Progress ──────────────────────────────────────────────

    def compute_progress(self, goal_id: int) -> dict:
        """Aggregate progress from tasks linked to this goal.

        Returns ``{total, completed, failed, running, pending, pct}``.
        """
        row = self._db.execute(
            "SELECT "
            "COUNT(*) as total, "
            "SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed, "
            "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed, "
            "SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running, "
            "SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending, "
            "COALESCE(SUM(cost_usd), 0) as total_cost "
            "FROM task_queue WHERE goal_id = ?",
            (goal_id,),
        ).fetchone()
        d = dict(row) if row else {
            "total": 0, "completed": 0, "failed": 0, "running": 0,
            "pending": 0, "total_cost": 0,
        }
        d["pct"] = round((d["completed"] / d["total"] * 100) if d["total"] else 0, 1)
        return d

    def get_children(self, goal_id: int) -> list[dict]:
        """Return direct sub-goals of a parent goal."""
        rows = self._db.execute(
            "SELECT * FROM goals WHERE parent_goal_id = ? ORDER BY created_at",
            (goal_id,),
        ).fetchall()
        return [dict(r) for r in rows]
