"""Software Factory — Plan / Build / Review orchestration.

Composes WorkflowEngine, TaskAPI, Orchestrator, and the approvals/goals
stores into a spec-first, review-gated delivery loop.

Invocation:
- Primary: orchestrator emits [BUILD] / [PLAN] / [REVIEW] tags which
  orchestrator._process_factory_requests() dispatches here.
- Secondary: HTTP `POST /api/v1/factory/{plan,build,review}` or
  `claude-daemon {plan,build,review} "<request>"` CLI commands.
"""
from __future__ import annotations

from claude_daemon.factory.config import FactoryConfig, ReviewPreset
from claude_daemon.factory.factory import SoftwareFactory
from claude_daemon.factory.models import (
    BuildResult,
    PlanResult,
    ReviewFinding,
    ReviewResult,
)

__all__ = [
    "BuildResult",
    "FactoryConfig",
    "PlanResult",
    "ReviewFinding",
    "ReviewPreset",
    "ReviewResult",
    "SoftwareFactory",
]
