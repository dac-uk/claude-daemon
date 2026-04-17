"""Budget management — caps, atomic reservation, period resets, spend recording.

Budgets have a *scope* (global, agent, user, task_type) and a *period*
(daily, weekly, monthly, lifetime).  Before a task is dispatched, the
enforcement layer calls ``check_and_reserve()`` which atomically increments
``current_spend`` only when the budget would not be exceeded.  After a task
completes, ``record_spend()`` adjusts the reservation to match the actual
cost reported by Claude.

Race protection: reservation uses a single UPDATE … WHERE guard so two
concurrent submits against a $1 budget can't both succeed.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)

VALID_SCOPES = {"global", "agent", "user", "task_type"}
VALID_PERIODS = {"daily", "weekly", "monthly", "lifetime"}


class BudgetStore:
    """CRUD + atomic reservation + spend recording on the ``budgets`` table."""

    def __init__(self, store: ConversationStore) -> None:
        self._db = store._db

    # ── CRUD ──────────────────────────────────────────────────

    def create(
        self,
        scope: str,
        limit_usd: float,
        period: str,
        scope_value: str | None = None,
        approval_threshold_usd: float | None = None,
    ) -> int:
        if scope not in VALID_SCOPES:
            raise ValueError(f"Invalid scope: {scope}")
        if period not in VALID_PERIODS:
            raise ValueError(f"Invalid period: {period}")
        if limit_usd <= 0:
            raise ValueError("limit_usd must be positive")

        reset_at = self._next_reset(period) if period != "lifetime" else None
        cur = self._db.execute(
            "INSERT INTO budgets "
            "(scope, scope_value, limit_usd, period, reset_at, approval_threshold_usd) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (scope, scope_value, limit_usd, period, reset_at, approval_threshold_usd),
        )
        self._db.commit()
        return cur.lastrowid

    def get(self, budget_id: int) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM budgets WHERE id = ?", (budget_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_all(self, enabled_only: bool = False) -> list[dict]:
        q = "SELECT * FROM budgets"
        if enabled_only:
            q += " WHERE enabled = 1"
        q += " ORDER BY scope, scope_value"
        return [dict(r) for r in self._db.execute(q).fetchall()]

    def update(
        self,
        budget_id: int,
        limit_usd: float | None = None,
        period: str | None = None,
        approval_threshold_usd: float | None = ...,
        enabled: bool | None = None,
    ) -> bool:
        row = self.get(budget_id)
        if row is None:
            return False
        if period is not None and period not in VALID_PERIODS:
            raise ValueError(f"Invalid period: {period}")
        if limit_usd is not None and limit_usd <= 0:
            raise ValueError("limit_usd must be positive")

        sets: list[str] = []
        params: list = []
        if limit_usd is not None:
            sets.append("limit_usd = ?")
            params.append(limit_usd)
        if period is not None:
            sets.append("period = ?")
            params.append(period)
            new_reset = self._next_reset(period) if period != "lifetime" else None
            sets.append("reset_at = ?")
            params.append(new_reset)
        if approval_threshold_usd is not ...:
            sets.append("approval_threshold_usd = ?")
            params.append(approval_threshold_usd)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(int(enabled))
        if not sets:
            return True

        params.append(budget_id)
        self._db.execute(
            f"UPDATE budgets SET {', '.join(sets)} WHERE id = ?", params,
        )
        self._db.commit()
        return True

    def delete(self, budget_id: int) -> bool:
        cur = self._db.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))
        self._db.commit()
        return cur.rowcount > 0

    # ── Query ─────────────────────────────────────────────────

    def get_applicable(
        self,
        agent_name: str | None = None,
        user_id: str | None = None,
        task_type: str | None = None,
    ) -> list[dict]:
        """Return all enabled budgets that apply to a given task context."""
        self._auto_reset_expired()
        results: list[dict] = []
        rows = self._db.execute(
            "SELECT * FROM budgets WHERE enabled = 1",
        ).fetchall()
        for r in rows:
            row = dict(r)
            s, sv = row["scope"], row["scope_value"]
            if s == "global":
                results.append(row)
            elif s == "agent" and sv == agent_name:
                results.append(row)
            elif s == "user" and sv == user_id:
                results.append(row)
            elif s == "task_type" and sv == task_type:
                results.append(row)
        return results

    # ── Atomic reservation ────────────────────────────────────

    def check_and_reserve(
        self,
        budget_id: int,
        amount: float,
    ) -> bool:
        """Atomically reserve ``amount`` against budget. Returns True if reserved.

        Uses UPDATE … WHERE to guarantee the budget is not exceeded even under
        concurrent writers (SQLite serialises writes via its WAL lock).
        """
        cur = self._db.execute(
            "UPDATE budgets SET current_spend = current_spend + ? "
            "WHERE id = ? AND enabled = 1 AND current_spend + ? <= limit_usd",
            (amount, budget_id, amount),
        )
        self._db.commit()
        return cur.rowcount > 0

    def release_reservation(self, budget_id: int, amount: float) -> None:
        """Undo a reservation (e.g. task was cancelled before it ran)."""
        self._db.execute(
            "UPDATE budgets SET current_spend = MAX(0, current_spend - ?) WHERE id = ?",
            (amount, budget_id),
        )
        self._db.commit()

    def release_reservations(self, reservations: list[tuple[int, float]]) -> None:
        """Release multiple reservations in a single transaction."""
        if not reservations:
            return
        for bid, amount in reservations:
            self._db.execute(
                "UPDATE budgets SET current_spend = MAX(0, current_spend - ?) WHERE id = ?",
                (amount, bid),
            )
        self._db.commit()

    def apply_actual_spend(
        self,
        reservations: list[tuple[int, float]],
        actual_cost: float,
    ) -> list[dict]:
        """Replace reservations with actual cost on each applicable budget.

        For each (budget_id, reserved_amount), applies a net delta of
        (actual_cost - reserved_amount). The delta may be negative (refund)
        when the task cost less than the reservation.

        Returns updated budget rows for broadcasting.
        """
        updated: list[dict] = []
        for bid, reserved in reservations:
            delta = actual_cost - reserved
            if abs(delta) < 1e-10:
                row = self.get(bid)
                if row:
                    updated.append(row)
                continue
            self._db.execute(
                "UPDATE budgets SET current_spend = MAX(0, current_spend + ?) WHERE id = ?",
                (delta, bid),
            )
            row = self.get(bid)
            if row:
                updated.append(row)
        if updated:
            self._db.commit()
        return updated

    # ── Post-completion spend recording ───────────────────────

    def record_spend(
        self,
        agent_name: str | None = None,
        user_id: str | None = None,
        task_type: str | None = None,
        actual_cost: float = 0.0,
    ) -> list[dict]:
        """Record actual spend against all applicable budgets.

        Returns list of budgets that were updated (with new current_spend).
        Called after task completion with the real cost from Claude.
        """
        if actual_cost <= 0:
            return []
        applicable = self.get_applicable(
            agent_name=agent_name, user_id=user_id, task_type=task_type,
        )
        updated = []
        for b in applicable:
            self._db.execute(
                "UPDATE budgets SET current_spend = current_spend + ? WHERE id = ?",
                (actual_cost, b["id"]),
            )
            b["current_spend"] += actual_cost
            updated.append(b)
        if updated:
            self._db.commit()
        return updated

    # ── Period resets ─────────────────────────────────────────

    def _auto_reset_expired(self) -> None:
        """Reset current_spend for budgets whose period has elapsed."""
        now = datetime.now(timezone.utc).isoformat()
        expired = self._db.execute(
            "SELECT id, period FROM budgets "
            "WHERE enabled = 1 AND period != 'lifetime' AND reset_at IS NOT NULL "
            "AND reset_at <= ?",
            (now,),
        ).fetchall()
        for row in expired:
            new_reset = self._next_reset(row["period"])
            self._db.execute(
                "UPDATE budgets SET current_spend = 0.0, reset_at = ? WHERE id = ?",
                (new_reset, row["id"]),
            )
        if expired:
            self._db.commit()
            log.info("Reset %d expired budget period(s)", len(expired))

    @staticmethod
    def _next_reset(period: str) -> str | None:
        now = datetime.now(timezone.utc)
        if period == "daily":
            dt = now + timedelta(days=1)
        elif period == "weekly":
            dt = now + timedelta(weeks=1)
        elif period == "monthly":
            dt = now + timedelta(days=30)
        else:
            return None
        return dt.isoformat()
