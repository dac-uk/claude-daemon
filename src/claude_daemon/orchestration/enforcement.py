"""Budget enforcement — pre-dispatch gate for task submission.

Called by ``TaskAPI.submit_task()`` before spawning.  Checks all applicable
budgets for the task context and returns a decision:

- ``allowed``  — no budget applies or all budgets have headroom
- ``rejected`` — at least one budget is exhausted
- ``approval_required`` — estimated cost exceeds an approval threshold
                          (Phase 4 will wire approval workflow)

Cost estimation: Claude cost only materialises after the response, so we use
a configurable minimum reservation (default $0.01) to reject requests when a
budget is already at its limit.  The actual spend is reconciled after task
completion via ``BudgetStore.record_spend()``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.orchestration.budgets import BudgetStore

log = logging.getLogger(__name__)

MIN_RESERVATION_USD = 0.01


@dataclass
class EnforcementDecision:
    outcome: str  # allowed | rejected | approval_required
    reason: str = ""
    blocked_by: list[dict] = field(default_factory=list)
    reservations: list[tuple[int, float]] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"


def enforce_budget(
    budget_store: BudgetStore,
    agent_name: str | None = None,
    user_id: str | None = None,
    task_type: str | None = None,
    estimated_cost: float = MIN_RESERVATION_USD,
) -> EnforcementDecision:
    """Check all applicable budgets and reserve headroom atomically.

    Returns an ``EnforcementDecision``.  If outcome is ``allowed``,
    ``reservations`` contains ``(budget_id, amount)`` pairs that were
    reserved and must be released if the task is cancelled before running.
    """
    applicable = budget_store.get_applicable(
        agent_name=agent_name,
        user_id=user_id,
        task_type=task_type,
    )

    if not applicable:
        return EnforcementDecision(outcome="allowed")

    amount = max(estimated_cost, MIN_RESERVATION_USD)
    reservations: list[tuple[int, float]] = []
    blocked: list[dict] = []

    for b in applicable:
        # Check approval threshold first (Phase 4 hook)
        threshold = b.get("approval_threshold_usd")
        if threshold is not None and amount >= threshold:
            # Roll back any reservations we've already made
            for bid, res_amt in reservations:
                budget_store.release_reservation(bid, res_amt)
            return EnforcementDecision(
                outcome="approval_required",
                reason=f"Estimated ${amount:.4f} exceeds approval threshold "
                       f"${threshold:.2f} on budget {b['id']}",
                blocked_by=[b],
            )

        ok = budget_store.check_and_reserve(b["id"], amount)
        if ok:
            reservations.append((b["id"], amount))
        else:
            blocked.append(b)

    if blocked:
        # Roll back partial reservations
        for bid, res_amt in reservations:
            budget_store.release_reservation(bid, res_amt)
        names = ", ".join(
            f"{b['scope']}:{b.get('scope_value', '*')} "
            f"(${b['current_spend']:.2f}/${b['limit_usd']:.2f})"
            for b in blocked
        )
        return EnforcementDecision(
            outcome="rejected",
            reason=f"Budget exceeded: {names}",
            blocked_by=blocked,
        )

    return EnforcementDecision(
        outcome="allowed",
        reservations=reservations,
    )
