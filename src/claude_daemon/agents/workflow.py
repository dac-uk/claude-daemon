"""WorkflowEngine - multi-step agent orchestration.

Enables sequential pipelines, parallel fan-out, and review loops
where agents chain work together with results flowing between steps.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.agents.orchestrator import Orchestrator
    from claude_daemon.agents.registry import AgentRegistry

log = logging.getLogger(__name__)


@dataclass
class WorkflowStep:
    """A single step in a workflow pipeline."""

    agent_name: str
    prompt_template: str  # May contain {prev_result}, {original_request}, {step_N_result}
    task_type: str = "workflow"
    label: str = ""  # Optional human-readable label for this step
    timeout: int = 600  # Per-step timeout in seconds (default 10 min)


@dataclass
class StepResult:
    """Result from a single workflow step."""

    agent_name: str
    label: str
    result: str
    cost: float = 0.0
    duration_ms: int = 0
    is_error: bool = False


@dataclass
class WorkflowResult:
    """Aggregate result of a complete workflow execution."""

    steps: list[StepResult] = field(default_factory=list)
    success: bool = True
    max_total_cost: float = 0.0  # 0 = unlimited

    @property
    def total_cost(self) -> float:
        return sum(s.cost for s in self.steps)

    @property
    def final_result(self) -> str:
        if self.steps:
            return self.steps[-1].result
        return ""

    def is_over_budget(self) -> bool:
        return self.max_total_cost > 0 and self.total_cost >= self.max_total_cost

    def summary(self) -> str:
        lines = []
        for i, step in enumerate(self.steps, 1):
            status = "PASS" if not step.is_error else "FAIL"
            dur = f" {step.duration_ms}ms" if step.duration_ms else ""
            lines.append(
                f"  {i}. [{status}] {step.agent_name}"
                f"{' (' + step.label + ')' if step.label else ''}"
                f" — ${step.cost:.4f}{dur}"
            )
        lines.append(f"  Total: ${self.total_cost:.4f}")
        if self.max_total_cost > 0:
            lines.append(f"  Budget: ${self.max_total_cost:.2f}")
        return "\n".join(lines)


class WorkflowEngine:
    """Execute multi-step agent workflows.

    Supports three execution patterns:
    - Pipeline: sequential steps, each receiving the previous result
    - Parallel: fan-out to multiple agents, collect all results
    - Review loop: build then review, retry on failure
    """

    def __init__(
        self,
        orchestrator: Orchestrator,
        registry: AgentRegistry,
    ) -> None:
        self.orchestrator = orchestrator
        self.registry = registry
        self._store = getattr(orchestrator, "store", None)

    def _checkpoint(
        self, workflow_id: str, workflow_type: str, steps: list[WorkflowStep],
        results: list[StepResult], current_step: int, original_request: str,
        max_total_cost: float, platform: str, user_id: str,
    ) -> None:
        """Persist workflow state so it can be resumed after a restart."""
        if not self._store:
            return
        import json
        steps_json = json.dumps([
            {"agent_name": s.agent_name, "prompt_template": s.prompt_template,
             "task_type": s.task_type, "label": s.label, "timeout": s.timeout}
            for s in steps
        ])
        results_json = json.dumps([
            {"agent_name": r.agent_name, "label": r.label, "result": r.result[:5000],
             "cost": r.cost, "duration_ms": r.duration_ms, "is_error": r.is_error}
            for r in results
        ])
        actual_cost = sum(r.cost for r in results)
        try:
            self._store.save_workflow_state(
                workflow_id=workflow_id, workflow_type=workflow_type,
                steps_json=steps_json, results_json=results_json,
                current_step=current_step, original_request=original_request,
                max_total_cost=max_total_cost, actual_cost=actual_cost,
                platform=platform, user_id=user_id,
            )
        except Exception:
            log.debug("Failed to checkpoint workflow %s", workflow_id)

    async def resume_pending_workflows(self) -> int:
        """Resume any workflows that were running when the daemon stopped."""
        if not self._store:
            return 0
        import json
        pending = self._store.get_pending_workflows()
        resumed = 0
        for wf in pending:
            try:
                steps = [WorkflowStep(**s) for s in json.loads(wf["steps_json"])]
                completed = [StepResult(**r) for r in json.loads(wf["results_json"])]
                current = wf["current_step"]
                remaining = steps[current:]
                if not remaining:
                    self._store.update_workflow_status(wf["workflow_id"], "completed")
                    continue
                log.info(
                    "Resuming workflow %s from step %d/%d",
                    wf["workflow_id"][:8], current + 1, len(steps),
                )
                asyncio.create_task(self._resume_pipeline(
                    wf["workflow_id"], steps, completed, current,
                    wf.get("original_request", ""),
                    wf.get("platform", "workflow"),
                    wf.get("user_id", "workflow"),
                    wf.get("max_total_cost", 0),
                ))
                resumed += 1
            except Exception:
                log.exception("Failed to resume workflow %s", wf.get("workflow_id", "?"))
                try:
                    self._store.update_workflow_status(wf["workflow_id"], "failed")
                except Exception:
                    pass
        return resumed

    async def _resume_pipeline(
        self, workflow_id: str, steps: list[WorkflowStep],
        completed: list[StepResult], start_step: int,
        original_request: str, platform: str, user_id: str,
        max_total_cost: float,
    ) -> None:
        """Resume a pipeline from a checkpoint."""
        remaining = steps[start_step:]
        prev_result = completed[-1].result if completed else ""
        result = WorkflowResult(steps=list(completed), max_total_cost=max_total_cost)
        step_results = {i: r.result for i, r in enumerate(completed)}

        for i, step in enumerate(remaining, start=start_step):
            if result.is_over_budget():
                break
            agent = self.registry.get(step.agent_name)
            if not agent:
                break
            prompt = step.prompt_template.format(
                original_request=original_request,
                prev_result=prev_result,
                **{f"step_{j}_result": r for j, r in step_results.items()},
            )
            start = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    self.orchestrator.send_to_agent(
                        agent=agent, prompt=prompt,
                        platform=platform, user_id=user_id, task_type=step.task_type,
                    ),
                    timeout=step.timeout,
                )
            except asyncio.TimeoutError:
                self._store.update_workflow_status(workflow_id, "failed")
                return
            duration = int((time.monotonic() - start) * 1000)
            sr = StepResult(
                agent_name=step.agent_name, label=step.label,
                result=response.result, cost=response.cost,
                duration_ms=duration, is_error=response.is_error,
            )
            result.steps.append(sr)
            step_results[i] = response.result
            prev_result = response.result
            self._checkpoint(
                workflow_id, "pipeline", steps, result.steps, i + 1,
                original_request, max_total_cost, platform, user_id,
            )
            if response.is_error:
                self._store.update_workflow_status(workflow_id, "failed")
                return

        self._store.update_workflow_status(workflow_id, "completed")
        log.info("Resumed workflow %s completed: %s", workflow_id[:8], result.summary())

    async def execute_pipeline(
        self,
        steps: list[WorkflowStep],
        original_request: str,
        platform: str = "workflow",
        user_id: str = "workflow",
        max_total_cost: float = 0.0,
        workflow_id: str | None = None,
    ) -> WorkflowResult:
        """Run steps sequentially. Each step's prompt can reference previous results.

        Template variables:
            {original_request} - The initial user request
            {prev_result} - Output from the immediately previous step
            {step_N_result} - Output from step N (0-indexed)
        """
        result = WorkflowResult(max_total_cost=max_total_cost)
        step_results: dict[int, str] = {}
        prev_result = ""

        for i, step in enumerate(steps):
            # Cost cap check
            if result.is_over_budget():
                sr = StepResult(
                    agent_name=step.agent_name, label=step.label,
                    result=f"Workflow cost cap exceeded (${result.total_cost:.2f} / ${max_total_cost:.2f})",
                    is_error=True,
                )
                result.steps.append(sr)
                result.success = False
                log.warning("Workflow cost cap reached at step %d: $%.2f", i, result.total_cost)
                break

            agent = self.registry.get(step.agent_name)
            if not agent:
                sr = StepResult(
                    agent_name=step.agent_name,
                    label=step.label,
                    result=f"Agent '{step.agent_name}' not found",
                    is_error=True,
                )
                result.steps.append(sr)
                result.success = False
                log.error("Workflow step %d: agent '%s' not found", i, step.agent_name)
                break

            prompt = step.prompt_template.format(
                original_request=original_request,
                prev_result=prev_result,
                **{f"step_{j}_result": r for j, r in step_results.items()},
            )

            log.info(
                "Workflow step %d/%d: %s (%s)",
                i + 1, len(steps), step.agent_name, step.label or step.task_type,
            )

            start = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    self.orchestrator.send_to_agent(
                        agent=agent, prompt=prompt,
                        platform=platform, user_id=user_id, task_type=step.task_type,
                    ),
                    timeout=step.timeout,
                )
            except asyncio.TimeoutError:
                duration = int((time.monotonic() - start) * 1000)
                sr = StepResult(
                    agent_name=step.agent_name, label=step.label,
                    result=f"Step timed out after {step.timeout}s",
                    duration_ms=duration, is_error=True,
                )
                result.steps.append(sr)
                result.success = False
                log.error("Workflow step %d timed out after %ds", i, step.timeout)
                break
            duration = int((time.monotonic() - start) * 1000)

            sr = StepResult(
                agent_name=step.agent_name,
                label=step.label,
                result=response.result,
                cost=response.cost,
                duration_ms=duration,
                is_error=response.is_error,
            )
            result.steps.append(sr)

            if response.is_error:
                result.success = False
                log.error("Workflow step %d failed: %s", i, response.result[:200])
                break

            step_results[i] = response.result
            prev_result = response.result

            if workflow_id:
                self._checkpoint(
                    workflow_id, "pipeline", steps, result.steps, i + 1,
                    original_request, max_total_cost, platform, user_id,
                )

        return result

    async def execute_parallel(
        self,
        steps: list[WorkflowStep],
        original_request: str,
        platform: str = "workflow",
        user_id: str = "workflow",
        max_total_cost: float = 0.0,
    ) -> WorkflowResult:
        """Run steps in parallel. All receive the original request. Per-step timeout enforced.

        Cost cap: a shared accumulator prevents new steps from starting once
        the budget is exceeded. Steps already in-flight are not cancelled.
        """
        result = WorkflowResult(max_total_cost=max_total_cost)
        cost_lock = asyncio.Lock()
        accumulated_cost = 0.0

        async def _run_step(step: WorkflowStep) -> StepResult:
            nonlocal accumulated_cost
            # Pre-check budget before invoking the agent
            if max_total_cost > 0:
                async with cost_lock:
                    if accumulated_cost >= max_total_cost:
                        return StepResult(
                            agent_name=step.agent_name, label=step.label,
                            result=f"Workflow cost cap exceeded (${accumulated_cost:.2f} / ${max_total_cost:.2f})",
                            is_error=True,
                        )

            agent = self.registry.get(step.agent_name)
            if not agent:
                return StepResult(
                    agent_name=step.agent_name, label=step.label,
                    result=f"Agent '{step.agent_name}' not found", is_error=True,
                )

            prompt = step.prompt_template.format(original_request=original_request)
            start = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    self.orchestrator.send_to_agent(
                        agent=agent, prompt=prompt,
                        platform=platform, user_id=user_id, task_type=step.task_type,
                    ),
                    timeout=step.timeout,
                )
            except asyncio.TimeoutError:
                duration = int((time.monotonic() - start) * 1000)
                return StepResult(
                    agent_name=step.agent_name, label=step.label,
                    result=f"Step timed out after {step.timeout}s",
                    duration_ms=duration, is_error=True,
                )
            duration = int((time.monotonic() - start) * 1000)

            # Post-update accumulated cost
            async with cost_lock:
                accumulated_cost += response.cost

            return StepResult(
                agent_name=step.agent_name, label=step.label,
                result=response.result, cost=response.cost,
                duration_ms=duration, is_error=response.is_error,
            )

        step_results = await asyncio.gather(*[_run_step(s) for s in steps])
        result.steps = list(step_results)
        result.success = all(not sr.is_error for sr in step_results)
        return result

    async def execute_review_loop(
        self,
        build_steps: list[WorkflowStep],
        review_step: WorkflowStep,
        original_request: str,
        max_iterations: int = 3,
        pass_keyword: str = "PASS",
        platform: str = "workflow",
        user_id: str = "workflow",
        max_total_cost: float = 0.0,
    ) -> WorkflowResult:
        """Build-review loop: execute build steps, then review. Retry on failure.

        The reviewer's response is checked for pass_keyword (case-insensitive).
        If not found, the build steps are re-run with the review feedback appended.
        Cost cap is checked before each iteration and before the review step.
        """
        all_results = WorkflowResult(max_total_cost=max_total_cost)

        for iteration in range(1, max_iterations + 1):
            # Cost cap check before each iteration
            if all_results.is_over_budget():
                log.warning("Review loop cost cap reached: $%.2f", all_results.total_cost)
                all_results.success = False
                break

            log.info("Review loop iteration %d/%d", iteration, max_iterations)

            # Run build pipeline (pass remaining budget)
            build_result = await self.execute_pipeline(
                build_steps, original_request, platform, user_id,
                max_total_cost=max_total_cost,
            )
            all_results.steps.extend(build_result.steps)

            if not build_result.success:
                all_results.success = False
                log.error("Build failed in review loop iteration %d", iteration)
                break

            # Cost cap check before review
            if all_results.is_over_budget():
                log.warning("Review loop cost cap reached before review: $%.2f", all_results.total_cost)
                all_results.success = False
                break

            # Run review
            build_output = build_result.final_result
            reviewer = self.registry.get(review_step.agent_name)
            if not reviewer:
                sr = StepResult(
                    agent_name=review_step.agent_name, label="review",
                    result=f"Reviewer '{review_step.agent_name}' not found",
                    is_error=True,
                )
                all_results.steps.append(sr)
                all_results.success = False
                break

            review_prompt = review_step.prompt_template.format(
                original_request=original_request,
                prev_result=build_output,
                build_output=build_output,
            )

            start = time.monotonic()
            review_response = await self.orchestrator.send_to_agent(
                agent=reviewer, prompt=review_prompt,
                platform=platform, user_id=user_id,
                task_type=review_step.task_type,
            )
            duration = int((time.monotonic() - start) * 1000)

            review_sr = StepResult(
                agent_name=review_step.agent_name, label=f"review (iteration {iteration})",
                result=review_response.result, cost=review_response.cost,
                duration_ms=duration, is_error=review_response.is_error,
            )
            all_results.steps.append(review_sr)

            if review_response.is_error:
                all_results.success = False
                break

            # Check if review passed
            if pass_keyword.lower() in review_response.result.lower():
                log.info("Review loop PASSED on iteration %d", iteration)
                all_results.success = True
                break

            # Review failed — append feedback for next iteration
            log.info("Review loop FAILED iteration %d, retrying", iteration)
            original_request = (
                f"{original_request}\n\n"
                f"--- Review feedback (iteration {iteration}) ---\n"
                f"{review_response.result}\n"
                f"--- Fix the issues above and try again ---"
            )

        else:
            # Exhausted max iterations
            log.warning("Review loop exhausted %d iterations", max_iterations)
            all_results.success = False

        return all_results

    # ------------------------------------------------------------------ #
    # Evo-powered code optimization
    # ------------------------------------------------------------------ #

    _EVO_PROMPT = """\
You have been asked to optimize code using evo (tree search over hill-climbing).

## Optimization Target
{target}

## Instructions
1. Identify a benchmark or test suite for the target (pytest, vitest, cargo test, etc.)
2. Run the baseline: measure current performance / pass rate
3. Use evo to explore variants:
   - evo will spawn parallel agents in git worktrees
   - Each variant is tested against the benchmark
   - Variants that improve the baseline are kept; others are discarded
4. Report results: what changed, baseline vs. result, which variant won

## Constraints
- Maximum {max_variants} parallel variants
- Do NOT commit changes that break existing tests
- If evo is not available, fall back to manual iteration

Begin by finding a suitable benchmark, then run the optimization.
"""

    async def execute_optimization(
        self,
        agent_name: str,
        target: str,
        max_budget: float = 0.0,
        max_variants: int = 3,
    ) -> WorkflowResult:
        """Run evo-powered code optimization as a single-step workflow.

        The target agent receives a structured prompt instructing it to use evo's
        tree search for parallel variant exploration with regression gates.
        """
        agent = self.registry.get(agent_name)
        if not agent:
            result = WorkflowResult(success=False)
            result.steps.append(StepResult(
                agent_name=agent_name, label="optimize",
                result=f"Agent '{agent_name}' not found", is_error=True,
            ))
            return result

        prompt = self._EVO_PROMPT.format(
            target=target.strip(),
            max_variants=max_variants,
        )

        log.info("Evo optimization: agent=%s target=%s", agent_name, target[:80])

        start = time.monotonic()
        response = await self.orchestrator.send_to_agent(
            agent=agent, prompt=prompt,
            platform="optimization", user_id="evo",
            task_type="workflow",
        )
        duration = int((time.monotonic() - start) * 1000)

        step = StepResult(
            agent_name=agent_name, label="evo-optimization",
            result=response.result, cost=response.cost,
            duration_ms=duration, is_error=response.is_error,
        )

        wf_result = WorkflowResult(
            steps=[step],
            success=not response.is_error,
            max_total_cost=max_budget,
        )

        if not response.is_error:
            log.info(
                "Evo optimization complete: agent=%s cost=$%.4f duration=%dms",
                agent_name, response.cost, duration,
            )
        else:
            log.warning("Evo optimization failed: agent=%s", agent_name)

        return wf_result
