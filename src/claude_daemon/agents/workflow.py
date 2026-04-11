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
    task_type: str = "default"
    label: str = ""  # Optional human-readable label for this step


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

    @property
    def total_cost(self) -> float:
        return sum(s.cost for s in self.steps)

    @property
    def final_result(self) -> str:
        if self.steps:
            return self.steps[-1].result
        return ""

    def summary(self) -> str:
        lines = []
        for i, step in enumerate(self.steps, 1):
            status = "PASS" if not step.is_error else "FAIL"
            lines.append(
                f"  {i}. [{status}] {step.agent_name}"
                f"{' (' + step.label + ')' if step.label else ''}"
                f" — ${step.cost:.4f}"
            )
        lines.append(f"  Total: ${self.total_cost:.4f}")
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

    async def execute_pipeline(
        self,
        steps: list[WorkflowStep],
        original_request: str,
        platform: str = "workflow",
        user_id: str = "workflow",
    ) -> WorkflowResult:
        """Run steps sequentially. Each step's prompt can reference previous results.

        Template variables:
            {original_request} - The initial user request
            {prev_result} - Output from the immediately previous step
            {step_N_result} - Output from step N (0-indexed)
        """
        result = WorkflowResult()
        step_results: dict[int, str] = {}
        prev_result = ""

        for i, step in enumerate(steps):
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

            # Build prompt from template
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
            response = await self.orchestrator.send_to_agent(
                agent=agent,
                prompt=prompt,
                platform=platform,
                user_id=user_id,
                task_type=step.task_type,
            )
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

        return result

    async def execute_parallel(
        self,
        steps: list[WorkflowStep],
        original_request: str,
        platform: str = "workflow",
        user_id: str = "workflow",
    ) -> WorkflowResult:
        """Run steps in parallel. All receive the original request."""
        result = WorkflowResult()

        async def _run_step(step: WorkflowStep) -> StepResult:
            agent = self.registry.get(step.agent_name)
            if not agent:
                return StepResult(
                    agent_name=step.agent_name, label=step.label,
                    result=f"Agent '{step.agent_name}' not found", is_error=True,
                )

            prompt = step.prompt_template.format(original_request=original_request)
            start = time.monotonic()
            response = await self.orchestrator.send_to_agent(
                agent=agent, prompt=prompt,
                platform=platform, user_id=user_id, task_type=step.task_type,
            )
            duration = int((time.monotonic() - start) * 1000)
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
    ) -> WorkflowResult:
        """Build-review loop: execute build steps, then review. Retry on failure.

        The reviewer's response is checked for pass_keyword (case-insensitive).
        If not found, the build steps are re-run with the review feedback appended.
        """
        all_results = WorkflowResult()

        for iteration in range(1, max_iterations + 1):
            log.info("Review loop iteration %d/%d", iteration, max_iterations)

            # Run build pipeline
            build_result = await self.execute_pipeline(
                build_steps, original_request, platform, user_id,
            )
            all_results.steps.extend(build_result.steps)

            if not build_result.success:
                all_results.success = False
                log.error("Build failed in review loop iteration %d", iteration)
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
