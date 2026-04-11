"""Tests for the WorkflowEngine."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_daemon.agents.agent import Agent, AgentIdentity
from claude_daemon.agents.registry import AgentRegistry
from claude_daemon.agents.workflow import (
    WorkflowEngine,
    WorkflowStep,
    WorkflowResult,
    StepResult,
)
from claude_daemon.core.process import ClaudeResponse


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


@pytest.fixture
def registry(agents_dir: Path) -> AgentRegistry:
    reg = AgentRegistry(agents_dir)
    reg.create_agent("albert", role="CIO")
    reg.create_agent("luna", role="Designer")
    reg.create_agent("max", role="CPO")
    return reg


@pytest.fixture
def mock_orchestrator(registry):
    orch = MagicMock()

    async def fake_send(agent, prompt, platform="workflow", user_id="workflow", task_type="default"):
        return ClaudeResponse(
            result=f"[{agent.name}] Done: {prompt[:50]}",
            session_id="test",
            cost=0.01,
            input_tokens=100,
            output_tokens=50,
            num_turns=1,
            duration_ms=500,
            is_error=False,
        )

    orch.send_to_agent = AsyncMock(side_effect=fake_send)
    return orch


@pytest.fixture
def engine(mock_orchestrator, registry):
    return WorkflowEngine(mock_orchestrator, registry)


@pytest.mark.asyncio
async def test_pipeline_basic(engine):
    steps = [
        WorkflowStep(agent_name="albert", prompt_template="Build backend for: {original_request}", label="backend"),
        WorkflowStep(agent_name="luna", prompt_template="Style UI based on: {prev_result}", label="frontend"),
    ]
    result = await engine.execute_pipeline(steps, "build a login page")
    assert result.success
    assert len(result.steps) == 2
    assert result.steps[0].agent_name == "albert"
    assert result.steps[1].agent_name == "luna"
    assert result.total_cost == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_pipeline_missing_agent(engine):
    steps = [
        WorkflowStep(agent_name="nonexistent", prompt_template="Do something"),
    ]
    result = await engine.execute_pipeline(steps, "test")
    assert not result.success
    assert result.steps[0].is_error
    assert "not found" in result.steps[0].result


@pytest.mark.asyncio
async def test_pipeline_stops_on_error(engine):
    async def failing_send(agent, prompt, **kwargs):
        if agent.name == "albert":
            return ClaudeResponse(
                result="Error occurred", session_id="test",
                cost=0.01, input_tokens=10, output_tokens=5,
                num_turns=1, duration_ms=100, is_error=True,
            )
        return ClaudeResponse(
            result="OK", session_id="test", cost=0.01,
            input_tokens=10, output_tokens=5, num_turns=1,
            duration_ms=100, is_error=False,
        )

    engine.orchestrator.send_to_agent = AsyncMock(side_effect=failing_send)

    steps = [
        WorkflowStep(agent_name="albert", prompt_template="Build"),
        WorkflowStep(agent_name="luna", prompt_template="Style: {prev_result}"),
    ]
    result = await engine.execute_pipeline(steps, "test")
    assert not result.success
    assert len(result.steps) == 1  # Luna was never called


@pytest.mark.asyncio
async def test_parallel_basic(engine):
    steps = [
        WorkflowStep(agent_name="albert", prompt_template="Backend: {original_request}", label="backend"),
        WorkflowStep(agent_name="luna", prompt_template="Frontend: {original_request}", label="frontend"),
    ]
    result = await engine.execute_parallel(steps, "build dashboard")
    assert result.success
    assert len(result.steps) == 2
    assert result.total_cost == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_review_loop_passes_first_try(engine):
    call_count = 0

    async def review_send(agent, prompt, **kwargs):
        nonlocal call_count
        call_count += 1
        result_text = f"[{agent.name}] Done" if agent.name != "max" else "Review: PASS - looks good"
        return ClaudeResponse(
            result=result_text, session_id="test", cost=0.01,
            input_tokens=100, output_tokens=50, num_turns=1,
            duration_ms=500, is_error=False,
        )

    engine.orchestrator.send_to_agent = AsyncMock(side_effect=review_send)

    build_steps = [
        WorkflowStep(agent_name="albert", prompt_template="Build: {original_request}"),
    ]
    review = WorkflowStep(
        agent_name="max",
        prompt_template="Review this: {build_output}",
    )
    result = await engine.execute_review_loop(build_steps, review, "build login")
    assert result.success
    assert len(result.steps) == 2  # 1 build + 1 review


@pytest.mark.asyncio
async def test_review_loop_retries_on_fail(engine):
    iteration = 0

    async def retry_send(agent, prompt, **kwargs):
        nonlocal iteration
        if agent.name == "max":
            iteration += 1
            if iteration < 2:
                return ClaudeResponse(
                    result="FAIL: needs more work", session_id="test",
                    cost=0.01, input_tokens=100, output_tokens=50,
                    num_turns=1, duration_ms=500, is_error=False,
                )
            return ClaudeResponse(
                result="PASS: looking good now", session_id="test",
                cost=0.01, input_tokens=100, output_tokens=50,
                num_turns=1, duration_ms=500, is_error=False,
            )
        return ClaudeResponse(
            result=f"[{agent.name}] built it", session_id="test",
            cost=0.01, input_tokens=100, output_tokens=50,
            num_turns=1, duration_ms=500, is_error=False,
        )

    engine.orchestrator.send_to_agent = AsyncMock(side_effect=retry_send)

    build_steps = [
        WorkflowStep(agent_name="albert", prompt_template="Build: {original_request}"),
    ]
    review = WorkflowStep(agent_name="max", prompt_template="Review: {build_output}")

    result = await engine.execute_review_loop(build_steps, review, "build it", max_iterations=3)
    assert result.success
    # 2 iterations: build1 + review1(fail) + build2 + review2(pass)
    assert len(result.steps) == 4


def test_workflow_result_summary():
    result = WorkflowResult(
        steps=[
            StepResult(agent_name="albert", label="backend", result="ok", cost=0.05),
            StepResult(agent_name="luna", label="frontend", result="ok", cost=0.03),
        ],
        success=True,
    )
    summary = result.summary()
    assert "albert" in summary
    assert "luna" in summary
    assert "PASS" in summary
    assert "$0.0800" in summary


def test_workflow_result_final():
    result = WorkflowResult(
        steps=[
            StepResult(agent_name="albert", label="", result="first"),
            StepResult(agent_name="luna", label="", result="final answer"),
        ],
    )
    assert result.final_result == "final answer"
