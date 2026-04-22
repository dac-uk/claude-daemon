"""SoftwareFactory — plan, build, review orchestration.

Composes existing primitives:
- Orchestrator.send_to_agent() for the planner turn
- WorkflowEngine.execute_review_loop() for build+review iterations
- WorkflowEngine.execute_parallel() for the reviewer fan-out
- TaskAPI.submit_task() for plan approval gating (single source of truth
  for task + approval rows — factory never calls ApprovalsStore.create()
  directly to avoid duplicate rows, per design audit Fix #2)

The factory never auto-creates Goals (audit Fix #4). Callers may pass an
explicit goal_id to link a factory run to an existing goal.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from claude_daemon.factory.config import FactoryConfig, ReviewPreset
from claude_daemon.factory.models import (
    BuildResult,
    PlanResult,
    ReviewFinding,
    ReviewResult,
)

if TYPE_CHECKING:
    from claude_daemon.agents.agent import Agent
    from claude_daemon.agents.orchestrator import Orchestrator
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.agents.workflow import WorkflowEngine
    from claude_daemon.memory.store import ConversationStore
    from claude_daemon.orchestration.approvals import ApprovalsStore
    from claude_daemon.orchestration.task_api import TaskAPI

log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(request: str, *, max_len: int = 40) -> str:
    base = _SLUG_RE.sub("-", request.lower()).strip("-")
    if not base:
        base = "request"
    if len(base) > max_len:
        base = base[:max_len].rstrip("-")
    # Timestamp gives human-readable ordering; the hex suffix guarantees
    # uniqueness under rapid / concurrent calls within the same second.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rand = secrets.token_hex(3)
    return f"{base}-{ts}-{rand}"


_SEVERITY_RE = re.compile(
    r"\b(CRITICAL|HIGH|MEDIUM|LOW)\b", re.IGNORECASE,
)

# Only accept git ref/path expressions matching this shape. Blocks
# option-style inputs (``--output=...``) that ``git diff`` would
# otherwise interpret as flags.
_SAFE_GIT_TARGET_RE = re.compile(r"^[A-Za-z0-9._/@^~:-]+$")


def _count_severities(text: str) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for match in _SEVERITY_RE.finditer(text):
        counts[match.group(1).lower()] += 1
    return counts


@dataclass
class _RoleAssignment:
    planner: Agent
    executors: list[Agent]
    reviewer: Agent


class SoftwareFactory:
    """Plan / Build / Review loop orchestration."""

    PLANNER_PROMPT = (
        "You are acting as the planner for the software factory.\n\n"
        "Feature request:\n{request}\n\n"
        "Write an implementation plan as a Markdown document with these "
        "sections:\n"
        "1. Context — what is being requested and why\n"
        "2. Approach — the proposed design at a high level\n"
        "3. Files to create or modify — bullet list with short rationale\n"
        "4. Risks — what could break, mitigations\n"
        "5. Verification — how success is confirmed\n\n"
        "Keep the plan concrete and under ~400 lines. Do NOT write code in "
        "this response — only the plan."
    )

    BUILD_STEP_PROMPT = (
        "Implement the feature described below.\n\n"
        "Original request: {original_request}\n\n"
        "{plan_section}"
        "Previous step output (empty for first step):\n{prev_result}\n\n"
        "Produce the actual changes: files edited, commands run, tests "
        "added. You MUST run tests before reporting completion. "
        "Include the test output in your response. At the end, summarise "
        "what you did, test results, and how to verify."
    )

    REVIEW_STEP_PROMPT = (
        "Review the build output below for quality, correctness, security, "
        "and adherence to the plan.\n\n"
        "Original request: {original_request}\n\n"
        "Build output:\n{build_output}\n\n"
        "{test_results}"
        "MANDATORY CHECKS before responding PASS:\n"
        "1. Were tests run? If test output is missing, respond FAIL.\n"
        "2. Did all tests pass? If any failed, respond FAIL.\n"
        "3. Does the implementation match the original request?\n"
        "4. If the project has CI (GitHub Actions), check the latest commit's "
        "CI status using your GitHub tools. If CI is red, respond FAIL.\n"
        "5. Are there any security issues, missing error handling, or untested paths?\n\n"
        "Respond with PASS on the first line if ALL checks pass, "
        "or FAIL on the first line followed by specific issues to fix. "
        "Include evidence: paste the test output or CI status URL."
    )

    REVIEW_PANEL_PROMPT = (
        "You are reviewing a diff with a focus on **{focus}**.\n\n"
        "{focus_instruction}\n\n"
        "Target: {target}\n\n"
        "Diff:\n```diff\n{diff}\n```\n\n"
        "Produce findings as a Markdown list. Each finding: severity tag "
        "(CRITICAL / HIGH / MEDIUM / LOW), file:line location, and a short "
        "description. If no issues found, respond with 'No {focus} issues "
        "found.'"
    )

    def __init__(
        self,
        orchestrator: Orchestrator,
        workflow_engine: WorkflowEngine,
        registry: AgentRegistry,
        store: ConversationStore,
        config: FactoryConfig,
        task_api: TaskAPI,
        approvals_store: ApprovalsStore | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.workflow = workflow_engine
        self.registry = registry
        self.store = store
        self.config = config
        self.task_api = task_api
        self.approvals_store = approvals_store
        self.config.plans_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Role resolution
    # ------------------------------------------------------------------ #

    def _resolve_agent(self, name: str, *, fallback_orchestrator: bool = False) -> Agent | None:
        """Look up an agent by name, honouring the 'auto' sentinel.

        'auto' picks the orchestrator when available, else the first agent.
        """
        if not name or name.lower() == "auto":
            orch = self.registry.get_orchestrator()
            if orch is not None:
                return orch
            agents = self.registry.list_agents()
            return agents[0] if agents else None

        agent = self.registry.get(name.lower())
        if agent is not None:
            return agent
        if fallback_orchestrator:
            return self.registry.get_orchestrator()
        return None

    def _resolve_roles(
        self,
        *,
        executor_override: list[str] | None = None,
    ) -> _RoleAssignment | None:
        planner = self._resolve_agent(
            self.config.planner_agent, fallback_orchestrator=True,
        )
        if planner is None:
            return None

        executor_names = executor_override or self.config.executor_agents
        executors: list[Agent] = []
        for name in executor_names:
            agent = self._resolve_agent(name)
            if agent is not None and agent not in executors:
                executors.append(agent)
        if not executors:
            # Fall back to planner as executor so we never return empty.
            executors = [planner]

        reviewer = self._resolve_agent(self.config.reviewer_agent)
        if reviewer is None:
            # Prefer an agent other than the sole executor as reviewer.
            others = [
                a for a in self.registry.list_agents()
                if a not in executors
            ]
            reviewer = others[0] if others else executors[0]

        if reviewer in executors:
            log.warning(
                "Factory reviewer is also an executor (%s) — the build "
                "will review itself. Consider adding a distinct reviewer "
                "agent to the registry or factory config.",
                reviewer.name,
            )

        return _RoleAssignment(
            planner=planner, executors=executors, reviewer=reviewer,
        )

    # ------------------------------------------------------------------ #
    # plan()
    # ------------------------------------------------------------------ #

    async def plan(
        self,
        request: str,
        *,
        platform: str = "cli",
        user_id: str = "local",
        goal_id: int | None = None,
        max_cost: float = 0.0,
    ) -> PlanResult:
        request = request.strip()
        if not request:
            return PlanResult(
                slug="", plan_path=Path(), plan_content="",
                status="error", error="Empty request",
            )

        slug = _slugify(request)
        plan_path = self.config.plans_dir / f"{slug}.md"

        roles = self._resolve_roles()
        if roles is None:
            return PlanResult(
                slug=slug, plan_path=plan_path, plan_content="",
                status="error", error="No planner agent available",
            )

        planner = roles.planner
        prompt = self.PLANNER_PROMPT.format(request=request)

        log.info("Factory.plan slug=%s planner=%s", slug, planner.name)
        self.store.record_audit(
            action="factory_plan_start", agent_name=planner.name,
            details=f"slug={slug}: {request[:160]}",
        )

        response = await self.orchestrator.send_to_agent(
            agent=planner, prompt=prompt,
            platform=platform, user_id=user_id, task_type="planning",
        )
        if response.is_error:
            return PlanResult(
                slug=slug, plan_path=plan_path, plan_content="",
                status="error", cost=response.cost,
                error=f"Planner error: {response.result[:200]}",
            )

        # Cost cap enforced AFTER the planner returns (the cost is only
        # knowable post-hoc). If over budget, fail without persisting.
        if max_cost and response.cost > max_cost:
            return PlanResult(
                slug=slug, plan_path=plan_path, plan_content=response.result,
                status="error", cost=response.cost,
                error=f"Plan cost ${response.cost:.4f} exceeds cap ${max_cost:.4f}",
            )

        # Audit-fix ordering: submit the TaskAPI row FIRST. Only write
        # the plan file to disk after a successful submit, so we never
        # leave orphan ``.md`` artefacts pointing at a task that never
        # got created.
        task_id = ""
        approval_id: int | None = None
        status = "pending"
        sub_error = ""
        if self.config.require_plan_approval:
            from claude_daemon.orchestration.task_api import TaskSubmission

            submission = TaskSubmission(
                prompt=f"Review plan at {plan_path}\n\n{response.result}",
                agent=planner.name,
                user_id=user_id,
                task_type="factory_plan",
                platform=platform,
                goal_id=goal_id,
                metadata={"factory_slug": slug, "plan_path": str(plan_path)},
                source="factory",
                require_approval=True,
            )
            submit_result = self.task_api.submit_task(submission)
            task_id = submit_result.task_id
            status = submit_result.status
            sub_error = submit_result.error or ""
            if status in ("rejected", "error"):
                # Do not write the plan file — surface the submit error.
                return PlanResult(
                    slug=slug, plan_path=plan_path, plan_content=response.result,
                    status=status, cost=response.cost,
                    error=sub_error or "Task submission failed",
                )
            if self.approvals_store and task_id and status == "pending_approval":
                row = self.approvals_store.get_by_task(task_id)
                if row:
                    approval_id = row["id"]

        header = (
            f"# Plan: {request}\n\n"
            f"_Generated {datetime.now(timezone.utc).isoformat()} by "
            f"{planner.name}._\n\n"
        )
        try:
            plan_path.write_text(header + response.result)
        except OSError as exc:
            log.exception("Failed to write plan file %s", plan_path)
            return PlanResult(
                slug=slug, plan_path=plan_path, plan_content=response.result,
                task_id=task_id, approval_id=approval_id,
                status="error", cost=response.cost,
                error=f"Plan file write failed: {exc}",
            )

        self.store.record_audit(
            action="factory_plan_complete", agent_name=planner.name,
            details=f"slug={slug} status={status} approval_id={approval_id}",
            cost_usd=response.cost, success=True,
        )

        return PlanResult(
            slug=slug,
            plan_path=plan_path,
            plan_content=response.result,
            task_id=task_id,
            approval_id=approval_id,
            status=status if status else "pending",
            goal_id=goal_id,
            cost=response.cost,
            error=sub_error,
        )

    # ------------------------------------------------------------------ #
    # build()
    # ------------------------------------------------------------------ #

    async def build(
        self,
        request: str,
        *,
        plan_path: Path | None = None,
        platform: str = "cli",
        user_id: str = "local",
        executor_agents: list[str] | None = None,
        goal_id: int | None = None,
        max_total_cost: float = 0.0,
        skip_plan: bool = False,
    ) -> BuildResult:
        request = request.strip()
        if not request:
            return BuildResult(
                slug="", success=False, error="Empty request",
            )

        slug = _slugify(request)

        # Auto-plan if no plan supplied (and not skipped). Auto-plan does
        # NOT require approval — it's an internal, inline step.
        plan_content = ""
        if plan_path is None and not skip_plan:
            roles_for_plan = self._resolve_roles(
                executor_override=executor_agents,
            )
            if roles_for_plan is None:
                return BuildResult(
                    slug=slug, success=False,
                    error="No planner agent available",
                )
            planner_response = await self.orchestrator.send_to_agent(
                agent=roles_for_plan.planner,
                prompt=self.PLANNER_PROMPT.format(request=request),
                platform=platform, user_id=user_id,
                task_type="planning",
            )
            if planner_response.is_error:
                return BuildResult(
                    slug=slug, success=False,
                    total_cost=planner_response.cost,
                    error=f"Planner failed: {planner_response.result[:200]}",
                )
            plan_content = planner_response.result
            plan_path = self.config.plans_dir / f"{slug}.md"
            plan_path.write_text(
                f"# Plan: {request}\n\n"
                f"_Auto-generated {datetime.now(timezone.utc).isoformat()}._"
                f"\n\n{plan_content}",
            )
        elif plan_path is not None:
            # Sandbox caller-supplied plan paths to ``plans_dir`` so an
            # HTTP/CLI caller can't point the factory at arbitrary files
            # (e.g. secret keys) and have their contents embedded in
            # the executor prompt.
            try:
                plans_root = self.config.plans_dir.resolve()
                resolved = plan_path.resolve()
                resolved.relative_to(plans_root)
            except (OSError, ValueError):
                return BuildResult(
                    slug=slug, success=False,
                    error=(
                        f"plan_path {plan_path} is outside "
                        f"plans_dir {self.config.plans_dir}"
                    ),
                )
            if not resolved.exists():
                return BuildResult(
                    slug=slug, success=False,
                    error=f"plan_path {plan_path} does not exist",
                )
            plan_path = resolved
            plan_content = plan_path.read_text()

        roles = self._resolve_roles(executor_override=executor_agents)
        if roles is None:
            return BuildResult(
                slug=slug, success=False,
                error="No executor/reviewer agents available",
            )

        # Escape braces in plan content so execute_pipeline's .format()
        # call doesn't try to substitute arbitrary {foo} tokens that
        # might appear inside the plan's code blocks.
        safe_plan = plan_content.replace("{", "{{").replace("}", "}}")
        plan_section = (
            f"Plan (from {plan_path}):\n{safe_plan}\n\n" if safe_plan else ""
        )

        from claude_daemon.agents.workflow import WorkflowStep

        build_steps = [
            WorkflowStep(
                agent_name=executor.name,
                prompt_template=self.BUILD_STEP_PROMPT.replace(
                    "{plan_section}", plan_section,
                ),
                label=f"build-{executor.name}",
                task_type="workflow",
            )
            for executor in roles.executors
        ]

        review_step = WorkflowStep(
            agent_name=roles.reviewer.name,
            prompt_template=self.REVIEW_STEP_PROMPT,
            label="review",
            task_type="workflow",
        )

        log.info(
            "Factory.build slug=%s executors=%s reviewer=%s goal_id=%s",
            slug, [a.name for a in roles.executors],
            roles.reviewer.name, goal_id,
        )
        goal_detail = f" goal_id={goal_id}" if goal_id is not None else ""
        self.store.record_audit(
            action="factory_build_start",
            agent_name=roles.executors[0].name,
            details=f"slug={slug}{goal_detail}: {request[:160]}",
        )

        result = await self.workflow.execute_review_loop(
            build_steps=build_steps,
            review_step=review_step,
            original_request=request,
            max_iterations=self.config.review_max_iterations,
            pass_keyword=self.config.review_pass_keyword,
            platform=platform,
            user_id=user_id,
            max_total_cost=max_total_cost,
        )

        result_path = self.config.plans_dir / f"{slug}-result.md"
        result_path.write_text(
            f"# Build Result: {request}\n\n"
            f"_Completed {datetime.now(timezone.utc).isoformat()}._\n\n"
            f"## Status\n{'PASS' if result.success else 'FAIL'}\n\n"
            f"## Steps\n{result.summary()}\n\n"
            f"## Final Output\n\n{result.final_result}\n",
        )

        # Count review iterations (steps labelled review-*)
        iterations = sum(
            1 for s in result.steps if s.label.startswith("review")
        )

        summary = (
            f"Build {'PASSED' if result.success else 'FAILED'} "
            f"in {iterations} review iteration(s), "
            f"cost ${result.total_cost:.4f}.\n"
            f"{result.summary()}"
        )

        self.store.record_audit(
            action="factory_build_complete",
            agent_name=roles.reviewer.name,
            details=(
                f"slug={slug} success={result.success} "
                f"iterations={iterations}{goal_detail}"
            ),
            cost_usd=result.total_cost, success=result.success,
        )

        return BuildResult(
            slug=slug,
            success=result.success,
            summary=summary,
            plan_path=plan_path,
            result_path=result_path,
            iterations=iterations,
            total_cost=result.total_cost,
            final_output=result.final_result,
            goal_id=goal_id,
        )

    # ------------------------------------------------------------------ #
    # review()
    # ------------------------------------------------------------------ #

    async def review(
        self,
        target: str | None = None,
        *,
        platform: str = "cli",
        user_id: str = "local",
        max_total_cost: float = 0.0,
    ) -> ReviewResult:
        effective_target = (
            target
            or self.config.default_review_target
            or "HEAD"
        )
        slug = _slugify(f"review-{effective_target}")

        diff = await self._generate_diff(effective_target)
        if not diff.strip():
            return ReviewResult(
                slug=slug,
                target=effective_target,
                summary="No diff to review.",
                error="empty-diff",
            )

        presets = self.config.review_presets or []
        if not presets:
            return ReviewResult(
                slug=slug, target=effective_target,
                summary="No review presets configured.",
                error="no-presets",
            )

        assigned = self._assign_review_agents(presets)
        if not assigned:
            return ReviewResult(
                slug=slug, target=effective_target,
                summary="No reviewer agents available.",
                error="no-agents",
            )

        from claude_daemon.agents.workflow import WorkflowStep

        # Truncate the diff when building the prompt so very large
        # changesets don't blow past per-step context limits.
        diff_for_prompt = diff[:20000]
        steps: list[WorkflowStep] = []
        preset_by_label: dict[str, ReviewPreset] = {}
        for preset, agent in assigned:
            label = f"review-{preset.focus}"
            preset_by_label[label] = preset
            prompt_template = self.REVIEW_PANEL_PROMPT.format(
                focus=preset.focus,
                focus_instruction=preset.prompt,
                target=effective_target,
                diff=diff_for_prompt,
            )
            # Escape every remaining brace so execute_parallel's
            # .format(original_request=...) doesn't trip on braces that
            # may appear inside the diff (function bodies, dicts, etc.).
            # execute_parallel will unescape them by calling .format().
            safe_prompt = (
                prompt_template.replace("{", "{{").replace("}", "}}")
            )
            steps.append(WorkflowStep(
                agent_name=agent.name,
                prompt_template=safe_prompt,
                label=label,
                task_type="workflow",
            ))

        log.info(
            "Factory.review slug=%s target=%s presets=%d",
            slug, effective_target, len(steps),
        )
        self.store.record_audit(
            action="factory_review_start", agent_name=assigned[0][1].name,
            details=f"slug={slug} target={effective_target} focuses={len(steps)}",
        )

        wf_result = await self.workflow.execute_parallel(
            steps=steps,
            original_request=effective_target,
            platform=platform,
            user_id=user_id,
            max_total_cost=max_total_cost,
        )

        findings: list[ReviewFinding] = []
        severity_counts = {
            "critical": 0, "high": 0, "medium": 0, "low": 0,
        }
        for step in wf_result.steps:
            preset = preset_by_label.get(step.label)
            focus = preset.focus if preset else step.label
            findings.append(ReviewFinding(
                focus=focus,
                agent_name=step.agent_name,
                content=step.result,
                cost=step.cost,
                is_error=step.is_error,
            ))
            if not step.is_error:
                for k, v in _count_severities(step.result).items():
                    severity_counts[k] += v

        report_path = self.config.plans_dir / f"{slug}-review.md"
        report_lines = [
            f"# Review Report: {effective_target}",
            "",
            f"_Generated {datetime.now(timezone.utc).isoformat()}._",
            "",
            "## Severity Counts",
            f"- Critical: {severity_counts['critical']}",
            f"- High: {severity_counts['high']}",
            f"- Medium: {severity_counts['medium']}",
            f"- Low: {severity_counts['low']}",
            "",
        ]
        for f in findings:
            report_lines.append(f"## {f.focus} — {f.agent_name}")
            report_lines.append("")
            report_lines.append(f.content)
            report_lines.append("")
        report_path.write_text("\n".join(report_lines))

        summary = (
            f"Review complete for {effective_target}: "
            f"{severity_counts['critical']} critical, "
            f"{severity_counts['high']} high, "
            f"{severity_counts['medium']} medium, "
            f"{severity_counts['low']} low. "
            f"Report: {report_path}"
        )

        self.store.record_audit(
            action="factory_review_complete",
            agent_name=assigned[0][1].name,
            details=(
                f"slug={slug} target={effective_target} "
                f"critical={severity_counts['critical']} "
                f"high={severity_counts['high']}"
            ),
            cost_usd=wf_result.total_cost, success=True,
        )

        return ReviewResult(
            slug=slug,
            target=effective_target,
            findings=findings,
            report_path=report_path,
            summary=summary,
            severity_counts=severity_counts,
            total_cost=wf_result.total_cost,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _assign_review_agents(
        self, presets: list[ReviewPreset],
    ) -> list[tuple[ReviewPreset, Agent]]:
        """Assign each preset to an agent, round-robining across available
        agents when explicit assignment isn't set."""
        agents = self.registry.list_agents()
        if not agents:
            return []

        explicit: list[tuple[ReviewPreset, Agent]] = []
        remaining_presets: list[ReviewPreset] = []
        for preset in presets:
            if preset.agent and preset.agent.lower() != "auto":
                agent = self.registry.get(preset.agent.lower())
                if agent is not None:
                    explicit.append((preset, agent))
                    continue
            remaining_presets.append(preset)

        if remaining_presets:
            # Prefer reviewers that aren't the orchestrator (if available)
            # so reviews spread across specialist agents.
            orch = self.registry.get_orchestrator()
            pool = [a for a in agents if a is not orch] or agents
            for i, preset in enumerate(remaining_presets):
                explicit.append((preset, pool[i % len(pool)]))

        return explicit

    async def _generate_diff(self, target: str) -> str:
        """Run `git diff` for the given target via asyncio subprocess.

        Returns an empty string on failure so callers can gracefully skip.
        """
        args = ["git", "diff"]
        t = target.strip()
        if t and t.upper() != "HEAD":
            if not _SAFE_GIT_TARGET_RE.match(t):
                log.warning("Refusing unsafe git diff target: %r", t)
                return ""
            # ``--`` prevents git from treating a valid-looking ref as
            # a flag if an exotic ref shape sneaks past the regex.
            args.extend(["--", t])

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=30,
                )
            except asyncio.TimeoutError:
                proc.kill()
                return ""
            return stdout.decode(errors="replace") if stdout else ""
        except FileNotFoundError:
            return ""
        except Exception:
            log.exception("git diff failed for target=%s", target)
            return ""
