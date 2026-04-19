"""Factory configuration — role assignment, review presets, policy."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_DEFAULT_PRESET_PROMPTS: dict[str, str] = {
    "bugs": (
        "Scan the diff below for correctness bugs: null/undefined handling, "
        "off-by-one errors, race conditions, incorrect error handling, and "
        "mismatches between the described intent and the actual behaviour. "
        "Flag specific file:line locations. Rate each finding LOW / MEDIUM / "
        "HIGH / CRITICAL."
    ),
    "security": (
        "Scan the diff below for security issues: injection vectors "
        "(SQL, command, XSS), missing authZ checks, secret leakage, unsafe "
        "deserialisation, insufficient input validation, CSRF, SSRF, path "
        "traversal. Flag specific file:line locations. Rate each finding "
        "LOW / MEDIUM / HIGH / CRITICAL."
    ),
    "performance": (
        "Scan the diff below for performance issues: N+1 queries, unbounded "
        "loops, missing indexes, synchronous I/O on hot paths, memory leaks, "
        "unbounded caches, needless allocations. Flag specific file:line "
        "locations. Rate each finding LOW / MEDIUM / HIGH."
    ),
    "quality": (
        "Scan the diff below for code-quality issues: unclear naming, dead "
        "code, duplication, missing tests for new logic, unclear error "
        "messages, violated style or architectural conventions. Flag "
        "specific file:line locations. Rate each finding LOW / MEDIUM / HIGH."
    ),
}


def _default_presets() -> list[ReviewPreset]:
    return [
        ReviewPreset(focus=focus, prompt=prompt)
        for focus, prompt in _DEFAULT_PRESET_PROMPTS.items()
    ]


@dataclass
class ReviewPreset:
    """One focus area in the parallel review fan-out."""

    focus: str               # bugs | security | performance | quality | ...
    prompt: str              # System instruction for this focus area
    agent: str = "auto"      # Explicit agent name, or "auto" to resolve


@dataclass
class FactoryConfig:
    """Centralised configuration for the Software Factory.

    Default values produce a working factory against any daemon with an
    orchestrator + at least one executor agent + one reviewer agent.
    """

    planner_agent: str = "orchestrator"
    executor_agents: list[str] = field(
        default_factory=lambda: ["auto"],
    )
    reviewer_agent: str = "auto"
    review_max_iterations: int = 3
    review_pass_keyword: str = "PASS"
    plans_dir: Path = field(
        default_factory=lambda: Path("shared/plans"),
    )
    test_command: str = ""
    require_plan_approval: bool = True
    auto_worktree: bool = False
    auto_pr: bool = False
    default_review_target: str = ""  # "" = current branch vs main
    review_presets: list[ReviewPreset] = field(
        default_factory=_default_presets,
    )

    @classmethod
    def from_dict(cls, d: dict[str, Any], data_dir: Path) -> FactoryConfig:
        plans_dir_raw = d.get("plans_dir")
        if plans_dir_raw:
            plans_dir = Path(plans_dir_raw)
            if not plans_dir.is_absolute():
                plans_dir = data_dir / plans_dir
        else:
            plans_dir = data_dir / "shared" / "plans"

        presets_raw = d.get("review_presets")
        if presets_raw:
            presets = [
                ReviewPreset(
                    focus=p["focus"],
                    prompt=p["prompt"],
                    agent=p.get("agent", "auto"),
                )
                for p in presets_raw
            ]
        else:
            presets = _default_presets()

        executors_raw = d.get("executor_agents")
        if isinstance(executors_raw, str):
            executors = [executors_raw]
        elif isinstance(executors_raw, list) and executors_raw:
            executors = list(executors_raw)
        else:
            executors = ["auto"]

        return cls(
            planner_agent=d.get("planner_agent", "orchestrator"),
            executor_agents=executors,
            reviewer_agent=d.get("reviewer_agent", "auto"),
            review_max_iterations=int(d.get("review_max_iterations", 3)),
            review_pass_keyword=d.get("review_pass_keyword", "PASS"),
            plans_dir=plans_dir,
            test_command=d.get("test_command", ""),
            require_plan_approval=bool(d.get("require_plan_approval", True)),
            auto_worktree=bool(d.get("auto_worktree", False)),
            auto_pr=bool(d.get("auto_pr", False)),
            default_review_target=d.get("default_review_target", ""),
            review_presets=presets,
        )
