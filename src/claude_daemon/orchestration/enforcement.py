"""Budget enforcement — pre-dispatch gate for task submission.

Called by ``TaskAPI.submit_task()`` before spawning.  Checks all applicable
budgets for the task context and returns a decision:

- ``allowed``  — no budget applies or all budgets have headroom
- ``rejected`` — at least one budget is exhausted (wins over approval_required)
- ``approval_required`` — estimated cost exceeds an approval threshold and
                          no budget is blocking

Cost estimation: Claude cost only materialises after the response, so we use
a configurable minimum reservation (default $0.01) to reject requests when a
budget is already at its limit.  The actual spend is reconciled after task
completion via ``BudgetStore.apply_actual_spend()``.

Evaluation order matters — **rejection wins**.  A task that both exhausts a
budget *and* trips an approval threshold is rejected outright.  Approval is
only returned when no hard cap blocks the request.
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
    threshold_usd: float | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == "allowed"


def enforce_budget(
    budget_store: BudgetStore,
    agent_name: str | None = None,
    user_id: str | None = None,
    task_type: str | None = None,
    estimated_cost: float = MIN_RESERVATION_USD,
    skip_approval_threshold: bool = False,
) -> EnforcementDecision:
    """Check all applicable budgets and reserve headroom atomically.

    Two-pass evaluation: rejection beats approval_required.  Pass 1 looks for
    any exhausted budget; if one exists, reject.  Pass 2 (unless
    ``skip_approval_threshold`` is True) looks for any threshold trigger and
    returns ``approval_required`` without reserving.  Pass 3 reserves atomically
    on every applicable budget and rolls back on contention.

    ``skip_approval_threshold`` is used on the approve path: the user has
    already approved, so we must not bounce the task back into the queue.
    """
    applicable = budget_store.get_applicable(
        agent_name=agent_name,
        user_id=user_id,
        task_type=task_type,
    )

    if not applicable:
        return EnforcementDecision(outcome="allowed")

    amount = max(estimated_cost, MIN_RESERVATION_USD)

    # Pass 1: rejection always wins over approval_required.
    blocked = [
        b for b in applicable
        if b["current_spend"] + amount > b["limit_usd"]
    ]
    if blocked:
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

    # Pass 2: threshold triggers — only if caller hasn't pre-approved.
    if not skip_approval_threshold:
        for b in applicable:
            threshold = b.get("approval_threshold_usd")
            if threshold is not None and amount >= threshold:
                return EnforcementDecision(
                    outcome="approval_required",
                    reason=f"Estimated ${amount:.4f} exceeds approval "
                           f"threshold ${threshold:.2f} on budget {b['id']}",
                    blocked_by=[b],
                    threshold_usd=threshold,
                )

    # Pass 3: atomically reserve against every applicable budget.
    reservations: list[tuple[int, float]] = []
    for b in applicable:
        ok = budget_store.check_and_reserve(b["id"], amount)
        if ok:
            reservations.append((b["id"], amount))
        else:
            # Raced — another writer drained it between pass 1 and pass 3.
            for bid, res_amt in reservations:
                budget_store.release_reservation(bid, res_amt)
            return EnforcementDecision(
                outcome="rejected",
                reason=f"Budget raced during reservation on {b['scope']}:"
                       f"{b.get('scope_value', '*')}",
                blocked_by=[b],
            )

    return EnforcementDecision(
        outcome="allowed",
        reservations=reservations,
    )
