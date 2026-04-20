"""Tests for the [STATUS:name] / [STATUS] inter-agent query tags."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_daemon.agents.orchestrator import (
    STATUS_ALL_PATTERN,
    STATUS_PATTERN,
    Orchestrator,
    _strip_code_blocks,
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
    reg.create_agent("luna", role="Designer")
    return reg


@pytest.fixture
def store(tmp_path: Path):
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def pm():
    pm = MagicMock()
    pm.active_count_for_agent = MagicMock(return_value=1)
    pm._sdk_bridge = MagicMock()
    return pm


def _resp(text: str) -> ClaudeResponse:
    return ClaudeResponse(
        result=text, session_id="test",
        cost=0.01, input_tokens=10, output_tokens=5,
        num_turns=1, duration_ms=100, is_error=False,
    )


# ── Regex Tests ──────────────────────────────────────────────────


class TestStatusPatterns:
    def test_status_pattern_matches_single_agent(self):
        text = "Let me check. [STATUS:albert]"
        matches = STATUS_PATTERN.findall(text)
        assert matches == ["albert"]

    def test_status_pattern_multiple_agents(self):
        text = "[STATUS:albert] [STATUS:luna]"
        matches = STATUS_PATTERN.findall(text)
        assert set(matches) == {"albert", "luna"}

    def test_status_all_pattern_matches(self):
        text = "Let me check the fleet. [STATUS]"
        assert STATUS_ALL_PATTERN.search(text) is not None

    def test_status_pattern_in_code_block_ignored(self):
        text = "```\n[STATUS:albert]\n```"
        stripped = _strip_code_blocks(text)
        matches = STATUS_PATTERN.findall(stripped)
        assert matches == []

    def test_status_pattern_in_inline_code_ignored(self):
        text = "Use `[STATUS:albert]` to check status."
        stripped = _strip_code_blocks(text)
        matches = STATUS_PATTERN.findall(stripped)
        assert matches == []

    def test_status_all_in_code_block_ignored(self):
        text = "```\n[STATUS]\n```"
        stripped = _strip_code_blocks(text)
        assert STATUS_ALL_PATTERN.search(stripped) is None

    def test_placeholder_names_not_matched(self):
        text = "[STATUS:name] [STATUS:agent_name] [STATUS:example]"
        matches = STATUS_PATTERN.findall(text)
        assert matches == []


# ── Orchestrator Integration Tests ───────────────────────────────


class TestProcessStatusQueries:
    @pytest.fixture
    def orchestrator(self, registry, pm, store):
        return Orchestrator(registry, pm, store)

    @pytest.mark.asyncio
    async def test_single_agent_status(self, orchestrator, pm, store):
        response = _resp("Checking albert's status. [STATUS:albert]")
        from_agent = orchestrator.registry.get("johnny")
        result = await orchestrator._process_status_queries(from_agent, response)
        assert "--- Status: albert ---" in result.result or "--- Status: Albert ---" in result.result
        assert "active" in result.result
        assert "$" in result.result

    @pytest.mark.asyncio
    async def test_agent_not_found(self, orchestrator):
        response = _resp("[STATUS:nonexistent]")
        from_agent = orchestrator.registry.get("johnny")
        result = await orchestrator._process_status_queries(from_agent, response)
        assert "agent not found" in result.result

    @pytest.mark.asyncio
    async def test_fleet_status(self, orchestrator, pm):
        response = _resp("Check everyone. [STATUS]")
        from_agent = orchestrator.registry.get("johnny")
        result = await orchestrator._process_status_queries(from_agent, response)
        assert "Fleet Status" in result.result
        assert "johnny" in result.result
        assert "albert" in result.result
        assert "luna" in result.result

    @pytest.mark.asyncio
    async def test_no_llm_call(self, orchestrator):
        """STATUS must not invoke agent_to_agent or send_to_agent."""
        orchestrator.agent_to_agent = AsyncMock()
        orchestrator.send_to_agent = AsyncMock()
        response = _resp("[STATUS:albert]")
        from_agent = orchestrator.registry.get("johnny")
        await orchestrator._process_status_queries(from_agent, response)
        orchestrator.agent_to_agent.assert_not_called()
        orchestrator.send_to_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_in_code_block_no_op(self, orchestrator):
        response = _resp("Example: ```[STATUS:albert]```")
        from_agent = orchestrator.registry.get("johnny")
        result = await orchestrator._process_status_queries(from_agent, response)
        assert "Status:" not in result.result.replace("Example:", "")

    @pytest.mark.asyncio
    async def test_status_works_in_discussion_platform(self, orchestrator, pm):
        """STATUS should be processed even during discussion context."""
        response = _resp("Before delegating, [STATUS:luna]")
        from_agent = orchestrator.registry.get("johnny")

        orchestrator.send_to_agent = AsyncMock(return_value=response)
        orchestrator.agent_to_agent = AsyncMock()

        result = await orchestrator._process_delegations(
            from_agent, response, platform="discussion",
        )
        assert "Status:" in result.result or "active" in result.result
