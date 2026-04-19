"""Result dataclasses for the Software Factory.

Kept separate from factory.py so tests + HTTP handlers can import the
shapes without pulling in the full orchestration dependency graph.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PlanResult:
    """Outcome of SoftwareFactory.plan()."""

    slug: str
    plan_path: Path
    plan_content: str
    task_id: str = ""
    approval_id: int | None = None
    status: str = "pending_approval"  # pending | pending_approval | rejected | error
    goal_id: int | None = None
    cost: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "plan_path": str(self.plan_path),
            "plan_content": self.plan_content,
            "task_id": self.task_id,
            "approval_id": self.approval_id,
            "status": self.status,
            "goal_id": self.goal_id,
            "cost": self.cost,
            "error": self.error,
        }


@dataclass
class BuildResult:
    """Outcome of SoftwareFactory.build()."""

    slug: str
    success: bool
    summary: str = ""
    plan_path: Path | None = None
    result_path: Path | None = None
    iterations: int = 0
    total_cost: float = 0.0
    final_output: str = ""
    goal_id: int | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "success": self.success,
            "summary": self.summary,
            "plan_path": str(self.plan_path) if self.plan_path else None,
            "result_path": str(self.result_path) if self.result_path else None,
            "iterations": self.iterations,
            "total_cost": self.total_cost,
            "final_output": self.final_output,
            "goal_id": self.goal_id,
            "error": self.error,
        }


@dataclass
class ReviewFinding:
    """A single reviewer's findings for one focus area."""

    focus: str
    agent_name: str
    content: str
    cost: float = 0.0
    is_error: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "focus": self.focus,
            "agent_name": self.agent_name,
            "content": self.content,
            "cost": self.cost,
            "is_error": self.is_error,
        }


@dataclass
class ReviewResult:
    """Outcome of SoftwareFactory.review()."""

    slug: str
    target: str
    findings: list[ReviewFinding] = field(default_factory=list)
    report_path: Path | None = None
    summary: str = ""
    severity_counts: dict[str, int] = field(default_factory=dict)
    total_cost: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "target": self.target,
            "findings": [f.to_dict() for f in self.findings],
            "report_path": str(self.report_path) if self.report_path else None,
            "summary": self.summary,
            "severity_counts": self.severity_counts,
            "total_cost": self.total_cost,
            "error": self.error,
        }
