"""Native orchestration layer for claude-daemon.

Provides task submission, cancellation, and lookup as a native subsystem
so the daemon can stand alone as a task-orchestration hub (replacing the
external Paperclip service for simple deployments).

Phase 1: task_api — submit/cancel/get/list_pending backed by the existing
task_queue SQLite table and Orchestrator.spawn_task().
"""
from claude_daemon.orchestration.task_api import (
    TaskAPI,
    TaskSubmission,
    TaskSubmissionResult,
)

__all__ = ["TaskAPI", "TaskSubmission", "TaskSubmissionResult"]
