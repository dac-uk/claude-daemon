"""Tests for the inter-agent discussion engine."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_daemon.agents.agent import Agent, AgentIdentity
from claude_daemon.agents.discussion import (
    DiscussionConfig,
    DiscussionEngine,
    DiscussionResult,
    DiscussionTurn,
)
from claude_daemon.agents.registry import AgentRegistry
from claude_daemon.core.process import ClaudeResponse
from claude_daemon.memory.store import ConversationStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    d = tmp_path / "shared"
    d.mkdir()
    (d / "discussions").mkdir()
    return d


@pytest.fixture
def store(tmp_path: Path):
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def registry(agents_dir: Path) -> AgentRegistry:
    reg = AgentRegistry(agents_dir)
    reg.create_agent("johnny", role="CEO", is_orchestrator=True)
    reg.create_agent("albert", role="CIO")
    reg.create_agent("luna", role="Designer")
    reg.create_agent("max", role="CPO")
    reg.create_agent("penny", role="CFO")
    return reg


@pytest.fixture
def mock_orchestrator(registry):
    orch = MagicMock()
    turn_counter = {"n": 0}

    async def fake_send(agent, prompt, **kwargs):
        turn_counter["n"] += 1
        content = f"[{agent.name}] Turn {turn_counter['n']}: I think we should proceed."
        if turn_counter["n"] >= 4:
            content += " CONSENSUS reached on this approach."
        return ClaudeResponse(
            result=content,
            session_id="test",
            cost=0.02,
            input_tokens=200,
            output_tokens=100,
            num_turns=1,
            duration_ms=500,
            is_error=False,
        )

    orch.send_to_agent = AsyncMock(side_effect=fake_send)
    orch.registry = registry
    return orch


@pytest.fixture
def config():
    cfg = MagicMock()
    cfg.discussions_enabled = True
    cfg.discussion_max_turns = 6
    cfg.discussion_max_cost = 1.00
    cfg.council_max_cost = 2.00
    cfg.council_max_rounds = 2
    return cfg


@pytest.fixture
def engine(mock_orchestrator, registry, store, config, shared_dir):
    return DiscussionEngine(
        mock_orchestrator, registry, store, config, shared_dir,
    )


# ---------------------------------------------------------------------------
# Bilateral discussion tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bilateral_basic(engine):
    result = await engine.run_bilateral("albert", "luna", "API design approach")
    assert result.outcome in ("completed", "converged", "max_turns")
    assert len(result.turns) >= 2
    assert result.total_cost > 0
    assert result.turns[0].agent_name == "albert"
    assert result.turns[1].agent_name == "luna"


@pytest.mark.asyncio
async def test_bilateral_alternates_speakers(engine):
    result = await engine.run_bilateral("albert", "luna", "Topic", max_turns=4, max_cost=5.0)
    speakers = [t.agent_name for t in result.turns]
    # Should alternate: albert, luna, albert, luna
    for i, name in enumerate(speakers):
        expected = "albert" if i % 2 == 0 else "luna"
        assert name == expected, f"Turn {i+1}: expected {expected}, got {name}"


@pytest.mark.asyncio
async def test_bilateral_converges_early(engine):
    """Discussion stops when CONSENSUS keyword is detected."""
    result = await engine.run_bilateral(
        "albert", "luna", "Quick question", max_turns=10, max_cost=5.0,
    )
    assert result.outcome == "converged"
    assert len(result.turns) < 10


@pytest.mark.asyncio
async def test_bilateral_cost_cap(engine):
    """Discussion stops when cost cap is exceeded."""
    result = await engine.run_bilateral(
        "albert", "luna", "Expensive topic",
        max_cost=0.03,  # Only enough for ~1 turn at $0.02 each
        max_turns=20,
    )
    assert result.outcome == "cost_exceeded"


@pytest.mark.asyncio
async def test_bilateral_records_to_db(engine, store):
    result = await engine.run_bilateral("albert", "luna", "Test topic")
    discussions = store.get_recent_discussions()
    assert len(discussions) >= 1
    assert discussions[0]["topic"] == "Test topic"
    assert discussions[0]["discussion_type"] == "bilateral"


@pytest.mark.asyncio
async def test_bilateral_writes_transcript_file(engine, shared_dir):
    await engine.run_bilateral("albert", "luna", "File test")
    disc_files = list((shared_dir / "discussions").glob("*.md"))
    assert len(disc_files) >= 1
    content = disc_files[0].read_text()
    assert "File test" in content
    assert "albert" in content


# ---------------------------------------------------------------------------
# Council tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_council_basic(engine):
    result = await engine.run_council("Should we adopt GraphQL?")
    assert result.config.discussion_type == "council"
    assert len(result.config.participants) >= 2
    assert result.total_cost > 0


@pytest.mark.asyncio
async def test_council_produces_synthesis(engine):
    """Council produces a synthesis from orchestrator."""
    result = await engine.run_council("Architecture decision")
    assert result.synthesis


@pytest.mark.asyncio
async def test_council_custom_participants(engine):
    result = await engine.run_council(
        "Budget review", participants=["penny", "johnny"],
    )
    assert set(result.config.participants) == {"penny", "johnny"}


@pytest.mark.asyncio
async def test_council_records_to_db(engine, store):
    result = await engine.run_council("Test council")
    discussions = store.get_recent_discussions(discussion_type="council")
    assert len(discussions) >= 1
    assert discussions[0]["discussion_type"] == "council"


# ---------------------------------------------------------------------------
# Data structure tests
# ---------------------------------------------------------------------------

def test_discussion_result_total_cost():
    result = DiscussionResult(
        discussion_id="test-001",
        config=DiscussionConfig(
            topic="test", initiator="albert",
            participants=["albert", "luna"],
            discussion_type="bilateral",
        ),
        turns=[
            DiscussionTurn(agent_name="albert", content="I think...", turn_number=1, cost=0.02),
            DiscussionTurn(agent_name="luna", content="I agree...", turn_number=2, cost=0.03),
        ],
    )
    assert result.total_cost == pytest.approx(0.05)


def test_discussion_result_transcript():
    result = DiscussionResult(
        discussion_id="test-002",
        config=DiscussionConfig(
            topic="test", initiator="albert",
            participants=["albert", "luna"],
            discussion_type="bilateral",
        ),
        turns=[
            DiscussionTurn(agent_name="albert", content="Point A", turn_number=1),
            DiscussionTurn(agent_name="luna", content="Point B", turn_number=2),
        ],
    )
    transcript = result.transcript
    assert "albert" in transcript
    assert "luna" in transcript
    assert "Point A" in transcript
    assert "Point B" in transcript


def test_discussion_result_over_budget():
    config = DiscussionConfig(
        topic="test", initiator="albert",
        participants=["albert", "luna"],
        discussion_type="bilateral",
        max_cost=0.04,
    )
    result = DiscussionResult(
        discussion_id="test-003",
        config=config,
        turns=[
            DiscussionTurn(agent_name="albert", content="...", turn_number=1, cost=0.03),
            DiscussionTurn(agent_name="luna", content="...", turn_number=2, cost=0.03),
        ],
    )
    assert result.is_over_budget()


def test_discussion_config_defaults():
    config = DiscussionConfig(
        topic="test", initiator="albert",
        participants=["albert", "luna"],
        discussion_type="bilateral",
    )
    assert config.max_turns == 6
    assert config.max_cost == 1.00
    assert config.convergence_keyword == "CONSENSUS"


# ---------------------------------------------------------------------------
# Tag regex tests
# ---------------------------------------------------------------------------

def test_discuss_pattern():
    from claude_daemon.agents.orchestrator import DISCUSS_PATTERN
    text = "[DISCUSS:luna] How should we handle the new dashboard layout?"
    matches = DISCUSS_PATTERN.findall(text)
    assert len(matches) == 1
    assert matches[0][0] == "luna"
    assert "dashboard layout" in matches[0][1]


def test_council_pattern():
    from claude_daemon.agents.orchestrator import COUNCIL_PATTERN
    text = "[COUNCIL] Should we migrate to GraphQL? This affects all agents."
    matches = COUNCIL_PATTERN.findall(text)
    assert len(matches) == 1
    assert "GraphQL" in matches[0]


def test_help_pattern():
    from claude_daemon.agents.orchestrator import HELP_PATTERN
    text = "[HELP:penny] What's our current monthly API spend?"
    matches = HELP_PATTERN.findall(text)
    assert len(matches) == 1
    # HELP_PATTERN captures (agent, flag, question); flag is optional.
    assert matches[0][0] == "penny"
    assert matches[0][1] == ""
    assert "API spend" in matches[0][2]


def test_multiple_tags_in_one_response():
    from claude_daemon.agents.orchestrator import (
        DISCUSS_PATTERN, HELP_PATTERN, DELEGATION_PATTERN,
    )
    text = (
        "Let me check costs first.\n"
        "[HELP:penny] Current monthly spend?\n"
        "[DISCUSS:albert] How should we restructure the API?\n"
        "[DELEGATE:luna] Please update the dashboard styles."
    )
    helps = HELP_PATTERN.findall(text)
    discusses = DISCUSS_PATTERN.findall(text)
    delegates = DELEGATION_PATTERN.findall(text)
    assert len(helps) == 1
    assert len(discusses) == 1
    assert len(delegates) == 1


def test_discuss_pattern_does_not_bleed_into_delegate():
    from claude_daemon.agents.orchestrator import DISCUSS_PATTERN
    text = "[DISCUSS:albert] API design[DELEGATE:luna] Build the UI"
    matches = DISCUSS_PATTERN.findall(text)
    assert len(matches) == 1
    assert "luna" not in matches[0][1]


# ---------------------------------------------------------------------------
# Store tests
# ---------------------------------------------------------------------------

def test_store_record_and_get_discussion(tmp_path: Path):
    s = ConversationStore(tmp_path / "test.db")
    s.record_discussion(
        discussion_id="disc-001",
        discussion_type="bilateral",
        topic="API design",
        initiator="albert",
        participants=["albert", "luna"],
        outcome="converged",
        total_turns=4,
        total_cost_usd=0.08,
        duration_ms=5000,
        synthesis="We agreed on REST.",
    )
    disc = s.get_discussion("disc-001")
    assert disc is not None
    assert disc["topic"] == "API design"
    assert disc["outcome"] == "converged"
    assert disc["total_cost_usd"] == pytest.approx(0.08)
    s.close()


def test_store_get_recent_discussions_filtered(tmp_path: Path):
    s = ConversationStore(tmp_path / "test.db")
    for i in range(5):
        s.record_discussion(
            discussion_id=f"disc-{i:03d}",
            discussion_type="council" if i % 2 == 0 else "bilateral",
            topic=f"Topic {i}",
            initiator="johnny",
            participants=["johnny", "albert"],
            outcome="completed",
            total_turns=2,
            total_cost_usd=0.05,
            duration_ms=1000,
        )
    all_discs = s.get_recent_discussions()
    assert len(all_discs) == 5
    councils = s.get_recent_discussions(discussion_type="council")
    assert len(councils) == 3
    s.close()


def test_store_discussion_stats(tmp_path: Path):
    s = ConversationStore(tmp_path / "test.db")
    s.record_discussion(
        discussion_id="disc-001",
        discussion_type="bilateral",
        topic="Test",
        initiator="albert",
        participants=["albert", "luna"],
        outcome="converged",
        total_turns=4,
        total_cost_usd=0.10,
        duration_ms=3000,
    )
    stats = s.get_discussion_stats(days=7)
    assert stats["total"] == 1
    assert stats["converged"] == 1
    assert stats["total_cost"] == pytest.approx(0.10)
    s.close()


# ---------------------------------------------------------------------------
# Action extraction tests
# ---------------------------------------------------------------------------

def test_extract_action_items_valid_json():
    from claude_daemon.agents.discussion import _extract_action_items
    synthesis = (
        "**Decision**: We should proceed.\n"
        "**Action Items**: Albert rotates secrets, Luna updates docs.\n"
        "```json\n"
        '{"actions": [\n'
        '  {"owner": "albert", "prompt": "Rotate all exposed API secrets", '
        '"priority": "high", "requires_approval": false},\n'
        '  {"owner": "luna", "prompt": "Update security docs", '
        '"priority": "medium", "requires_approval": true}\n'
        "]}\n"
        "```\n"
    )
    actions = _extract_action_items(synthesis)
    assert len(actions) == 2
    assert actions[0]["owner"] == "albert"
    assert actions[0]["priority"] == "high"
    assert actions[0]["requires_approval"] is False
    assert actions[1]["owner"] == "luna"
    assert actions[1]["requires_approval"] is True


def test_extract_action_items_malformed_json():
    from claude_daemon.agents.discussion import _extract_action_items
    synthesis = "```json\n{bad json here}\n```"
    actions = _extract_action_items(synthesis)
    assert actions == []


def test_extract_action_items_empty_actions():
    from claude_daemon.agents.discussion import _extract_action_items
    synthesis = '```json\n{"actions": []}\n```'
    actions = _extract_action_items(synthesis)
    assert actions == []


def test_extract_action_items_missing_block():
    from claude_daemon.agents.discussion import _extract_action_items
    synthesis = "Decision: No actions needed. Just monitor."
    actions = _extract_action_items(synthesis)
    assert actions == []


def test_extract_action_items_skips_malformed_entries():
    from claude_daemon.agents.discussion import _extract_action_items
    synthesis = (
        '```json\n'
        '{"actions": [{"owner": "albert", "prompt": "Do work"}, '
        '"not-a-dict", {"owner": "", "prompt": "missing owner"}, '
        '{"owner": "luna"}]}\n'
        '```'
    )
    actions = _extract_action_items(synthesis)
    assert len(actions) == 1
    assert actions[0]["owner"] == "albert"


def test_extract_action_items_truncates_long_prompt():
    from claude_daemon.agents.discussion import _extract_action_items
    synthesis = (
        '```json\n'
        '{"actions": [{"owner": "albert", "prompt": "' + "x" * 3000 + '"}]}\n'
        '```'
    )
    actions = _extract_action_items(synthesis)
    assert len(actions) == 1
    assert len(actions[0]["prompt"]) == 2000


@pytest.mark.asyncio
async def test_spawn_council_actions_creates_tasks(engine, store, config):
    """Council auto-spawns tasks from structured synthesis actions."""
    config.council_auto_execute_actions = True
    config.council_max_actions_per_run = 8

    # Give the engine a task_api
    from claude_daemon.orchestration.task_api import TaskAPI
    task_api = TaskAPI(
        orchestrator=engine.orchestrator,
        registry=engine.registry,
        store=store,
    )
    engine.task_api = task_api

    result = DiscussionResult(
        discussion_id="council-test-001",
        config=DiscussionConfig(
            topic="Security plan",
            initiator="albert",
            participants=["albert", "luna"],
            discussion_type="council",
        ),
        synthesis=(
            "Decision: Rotate secrets.\n"
            '```json\n'
            '{"actions": [\n'
            '  {"owner": "albert", "prompt": "Rotate all API keys", "priority": "high"},\n'
            '  {"owner": "luna", "prompt": "Update documentation", "priority": "medium"}\n'
            ']}\n'
            '```'
        ),
    )
    task_ids = await engine._spawn_council_actions(result)
    assert len(task_ids) == 2

    # Verify tasks are in the DB with source=council
    for tid in task_ids:
        task = store.get_task(tid)
        assert task is not None
        assert task["source"] == "council"


@pytest.mark.asyncio
async def test_spawn_council_actions_off_switch(engine, config):
    """No tasks spawned when council_auto_execute_actions=False."""
    config.council_auto_execute_actions = False

    result = DiscussionResult(
        discussion_id="council-off-001",
        config=DiscussionConfig(
            topic="Test", initiator="albert",
            participants=["albert"], discussion_type="council",
        ),
        synthesis='```json\n{"actions": [{"owner": "albert", "prompt": "Do work"}]}\n```',
    )
    task_ids = await engine._spawn_council_actions(result)
    assert task_ids == []


@pytest.mark.asyncio
async def test_spawn_council_actions_owner_not_participant(engine, store, config):
    """Actions for non-participants are skipped."""
    config.council_auto_execute_actions = True
    config.council_max_actions_per_run = 8

    from claude_daemon.orchestration.task_api import TaskAPI
    engine.task_api = TaskAPI(
        orchestrator=engine.orchestrator,
        registry=engine.registry,
        store=store,
    )

    result = DiscussionResult(
        discussion_id="council-owner-001",
        config=DiscussionConfig(
            topic="Test", initiator="albert",
            participants=["albert"],
            discussion_type="council",
        ),
        synthesis='```json\n{"actions": [{"owner": "intruder", "prompt": "Evil task"}]}\n```',
    )
    task_ids = await engine._spawn_council_actions(result)
    assert task_ids == []


@pytest.mark.asyncio
async def test_spawn_council_actions_caps_at_max(engine, store, config):
    """Action count is capped at council_max_actions_per_run."""
    config.council_auto_execute_actions = True
    config.council_max_actions_per_run = 2

    from claude_daemon.orchestration.task_api import TaskAPI
    engine.task_api = TaskAPI(
        orchestrator=engine.orchestrator,
        registry=engine.registry,
        store=store,
    )

    actions_json = ", ".join(
        f'{{"owner": "albert", "prompt": "Task {i}"}}'
        for i in range(5)
    )
    result = DiscussionResult(
        discussion_id="council-cap-001",
        config=DiscussionConfig(
            topic="Test", initiator="albert",
            participants=["albert"],
            discussion_type="council",
        ),
        synthesis=f'```json\n{{"actions": [{actions_json}]}}\n```',
    )
    task_ids = await engine._spawn_council_actions(result)
    assert len(task_ids) == 2


def test_store_update_discussion_post_synthesis(tmp_path: Path):
    """update_discussion_post_synthesis persists synthesis and task IDs."""
    s = ConversationStore(tmp_path / "test.db")
    s.record_discussion(
        discussion_id="disc-synth-001",
        discussion_type="council",
        topic="Test council",
        initiator="albert",
        participants=["albert", "luna"],
        outcome="converged",
        total_turns=4,
        total_cost_usd=0.12,
        duration_ms=3000,
    )
    s.update_discussion_post_synthesis(
        discussion_id="disc-synth-001",
        synthesis="Updated synthesis text.",
        action_task_ids=["task-abc", "task-def"],
    )
    disc = s.get_discussion("disc-synth-001")
    assert disc is not None
    assert disc["synthesis"] == "Updated synthesis text."
    import json
    assert json.loads(disc["action_task_ids"]) == ["task-abc", "task-def"]
    s.close()
