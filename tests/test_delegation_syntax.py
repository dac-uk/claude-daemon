"""Tests for [DELEGATE:agent:flag] syntax + post-hoc model-routing audit.

Covers:
- DELEGATION_PATTERN with and without the optional flag group.
- _resolve_task_type mapping from flag → task_type with warnings for unknowns.
- _process_delegations routing the resolved task_type into agent_to_agent.
- compute_delegation_audit rubric outcomes (appropriate / over / under-routed).
- record_delegation_audit persisting rows into the delegation_audit table.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_daemon.agents.orchestrator import (
    DELEGATION_PATTERN,
    Orchestrator,
    _resolve_task_type,
    compute_delegation_audit,
)
from claude_daemon.agents.registry import AgentRegistry
from claude_daemon.core.process import ClaudeResponse
from claude_daemon.memory.store import ConversationStore


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


@pytest.fixture
def registry(agents_dir: Path) -> AgentRegistry:
    reg = AgentRegistry(agents_dir)
    reg.create_agent("johnny", role="CEO", is_orchestrator=True)
    reg.create_agent("albert", role="CIO")
    return reg


@pytest.fixture
def store(tmp_path: Path):
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def pm():
    p = MagicMock()
    p.active_count_for_agent = MagicMock(return_value=0)
    p._sdk_bridge = MagicMock()
    return p


@pytest.fixture
def orchestrator(registry, pm, store):
    return Orchestrator(registry, pm, store)


def _resp(text: str) -> ClaudeResponse:
    return ClaudeResponse(
        result=text, session_id="test",
        cost=0.01, input_tokens=10, output_tokens=5,
        num_turns=1, duration_ms=100, is_error=False,
    )


# ── DELEGATION_PATTERN regex tests ────────────────────────────────


class TestDelegationPattern:
    def test_matches_without_flag(self):
        matches = DELEGATION_PATTERN.findall("[DELEGATE:albert] do the thing")
        assert len(matches) == 1
        agent, flag, message = matches[0]
        assert agent == "albert"
        assert flag == ""
        assert "do the thing" in message

    def test_matches_complex_flag(self):
        matches = DELEGATION_PATTERN.findall("[DELEGATE:albert:complex] big job")
        assert len(matches) == 1
        agent, flag, message = matches[0]
        assert agent == "albert"
        assert flag == "complex"
        assert "big job" in message

    def test_matches_plan_flag(self):
        matches = DELEGATION_PATTERN.findall("[DELEGATE:albert:plan] design it")
        assert len(matches) == 1
        agent, flag, message = matches[0]
        assert agent == "albert"
        assert flag == "plan"
        assert "design it" in message

    def test_placeholder_names_ignored(self):
        matches = DELEGATION_PATTERN.findall("[DELEGATE:agent_name] example")
        assert matches == []


# ── _resolve_task_type tests ──────────────────────────────────────


class TestResolveTaskType:
    def test_none_flag_returns_default(self):
        assert _resolve_task_type(None) == "default"

    def test_empty_flag_returns_default(self):
        assert _resolve_task_type("") == "default"

    def test_simple_flag_returns_default(self):
        assert _resolve_task_type("simple") == "default"

    def test_complex_flag_returns_planning(self):
        assert _resolve_task_type("complex") == "planning"

    def test_plan_flag_returns_planning(self):
        assert _resolve_task_type("plan") == "planning"

    def test_planning_flag_returns_planning(self):
        assert _resolve_task_type("planning") == "planning"

    def test_unknown_flag_logs_warning_and_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING, logger="claude_daemon.agents.orchestrator"):
            result = _resolve_task_type("foo")
        assert result == "default"
        assert any("Unknown delegation flag" in r.message and "foo" in r.message
                   for r in caplog.records)


# ── _process_delegations routing tests ────────────────────────────


class TestProcessDelegations:
    @pytest.mark.asyncio
    async def test_complex_flag_routes_to_planning(self, orchestrator):
        orchestrator.agent_to_agent = AsyncMock(return_value="ok")
        from_agent = orchestrator.registry.get("johnny")
        response = _resp("Handing off. [DELEGATE:albert:complex] refactor X")

        await orchestrator._process_delegations(from_agent, response)

        orchestrator.agent_to_agent.assert_awaited_once()
        kwargs = orchestrator.agent_to_agent.await_args.kwargs
        assert kwargs["task_type"] == "planning"

    @pytest.mark.asyncio
    async def test_no_flag_routes_to_default(self, orchestrator):
        orchestrator.agent_to_agent = AsyncMock(return_value="ok")
        from_agent = orchestrator.registry.get("johnny")
        response = _resp("[DELEGATE:albert] quick edit")

        await orchestrator._process_delegations(from_agent, response)

        orchestrator.agent_to_agent.assert_awaited_once()
        kwargs = orchestrator.agent_to_agent.await_args.kwargs
        assert kwargs["task_type"] == "default"

    @pytest.mark.asyncio
    async def test_plan_flag_routes_to_planning(self, orchestrator):
        orchestrator.agent_to_agent = AsyncMock(return_value="ok")
        from_agent = orchestrator.registry.get("johnny")
        response = _resp("[DELEGATE:albert:plan] design it")

        await orchestrator._process_delegations(from_agent, response)

        orchestrator.agent_to_agent.assert_awaited_once()
        kwargs = orchestrator.agent_to_agent.await_args.kwargs
        assert kwargs["task_type"] == "planning"

    @pytest.mark.asyncio
    async def test_unknown_flag_falls_back_and_warns(self, orchestrator, caplog):
        orchestrator.agent_to_agent = AsyncMock(return_value="ok")
        from_agent = orchestrator.registry.get("johnny")
        response = _resp("[DELEGATE:albert:foo] some task")

        with caplog.at_level(logging.WARNING, logger="claude_daemon.agents.orchestrator"):
            await orchestrator._process_delegations(from_agent, response)

        kwargs = orchestrator.agent_to_agent.await_args.kwargs
        assert kwargs["task_type"] == "default"
        assert any("Unknown delegation flag" in r.message and "foo" in r.message
                   for r in caplog.records)


# ── compute_delegation_audit rubric tests ─────────────────────────


class TestComputeDelegationAudit:
    def test_over_routed_complex_work_on_sonnet(self):
        # files_changed=5 (≥3), loc_delta=200 (≥100) → 2 criteria tripped
        audit = compute_delegation_audit(
            files_changed=5,
            loc_delta=200,
            new_test_files=False,
            task_type="default",
            model_used="claude-sonnet-4-6",
            prompt="fix the thing",
        )
        assert audit["tripped"] >= 2
        assert audit["expected"] == "opus"
        assert audit["used"] == "sonnet"
        assert audit["outcome"] == "over-routed"

    def test_under_routed_trivial_work_on_opus(self):
        # 1 file, 10 LOC, no tests, task_type=default, no keyword → 0 criteria
        audit = compute_delegation_audit(
            files_changed=1,
            loc_delta=10,
            new_test_files=False,
            task_type="default",
            model_used="claude-opus-4-7",
            prompt="fix typo",
        )
        assert audit["tripped"] < 2
        assert audit["expected"] == "sonnet"
        assert audit["used"] == "opus"
        assert audit["outcome"] == "under-routed"

    def test_appropriate_complex_work_on_opus(self):
        audit = compute_delegation_audit(
            files_changed=5,
            loc_delta=300,
            new_test_files=True,
            task_type="planning",
            model_used="claude-opus-4-7",
            prompt="refactor the memory store",
        )
        assert audit["expected"] == "opus"
        assert audit["used"] == "opus"
        assert audit["outcome"] == "appropriate"

    def test_appropriate_trivial_work_on_sonnet(self):
        audit = compute_delegation_audit(
            files_changed=1,
            loc_delta=5,
            new_test_files=False,
            task_type="default",
            model_used="claude-sonnet-4-6",
            prompt="rename variable",
        )
        assert audit["expected"] == "sonnet"
        assert audit["used"] == "sonnet"
        assert audit["outcome"] == "appropriate"


# ── record_delegation_audit persistence tests ─────────────────────


class TestRecordDelegationAudit:
    def test_persists_row(self, store):
        store.record_delegation_audit(
            task_id="abc123",
            agent_name="albert",
            task_type_used="planning",
            model_used="claude-opus-4-7",
            tripped_count=3,
            outcome="appropriate",
            files_changed=5,
            loc_delta=200,
            new_test_files=True,
            prompt_sample="refactor the memory store" * 50,
        )
        rows = store.get_delegation_audits(agent_name="albert")
        assert len(rows) == 1
        row = rows[0]
        assert row["task_id"] == "abc123"
        assert row["task_type_used"] == "planning"
        assert row["outcome"] == "appropriate"
        assert row["new_test_files"] == 1
        # prompt_sample is truncated to 200 chars
        assert len(row["prompt_sample"]) <= 200

    def test_filter_by_outcome(self, store):
        store.record_delegation_audit(
            task_id="t1", agent_name="albert",
            task_type_used="default", model_used="sonnet",
            tripped_count=0, outcome="appropriate",
        )
        store.record_delegation_audit(
            task_id="t2", agent_name="albert",
            task_type_used="default", model_used="sonnet",
            tripped_count=3, outcome="over-routed",
        )
        over = store.get_delegation_audits(outcome="over-routed")
        assert len(over) == 1
        assert over[0]["task_id"] == "t2"
