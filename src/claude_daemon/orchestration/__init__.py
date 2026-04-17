"""Native orchestration layer for claude-daemon.

Provides task submission, cancellation, and lookup as a native subsystem
so the daemon can stand alone as a task-orchestration hub (replacing the
external Paperclip service for simple deployments).

Phase 1: task_api — submit/cancel/get/list_pending
Phase 2: budgets — caps, enforcement, atomic reservation, spend recording
Phase 3: goals — tracking, progress aggregation, task linking
Phase 4: approvals — gate high-cost tasks behind human review
"""
from claude_daemon.orchestration.approvals import ApprovalsStore
from claude_daemon.orchestration.budgets import BudgetStore
from claude_daemon.orchestration.enforcement import EnforcementDecision, enforce_budget
from claude_daemon.orchestration.goals import GoalsStore
from claude_daemon.orchestration.task_api import (
    TaskAPI,
    TaskSubmission,
    TaskSubmissionResult,
)

__all__ = [
    "TaskAPI",
    "TaskSubmission",
    "TaskSubmissionResult",
    "BudgetStore",
    "EnforcementDecision",
    "enforce_budget",
    "GoalsStore",
    "ApprovalsStore",
]
