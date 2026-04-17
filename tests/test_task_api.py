"""Tests for the native task API (orchestration.task_api)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_daemon.memory.store import ConversationStore
from claude_daemon.orchestration import TaskAPI, TaskSubmission


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


def _make_agent(name: str) -> MagicMock:
    agent = MagicMock()
    agent.name = name
    return agent


@pytest.fixture
def registry_with_agents():
    """Mock AgentRegistry that returns agents by name."""
    agents = {
        "albert": _make_agent("albert"),
        "johnny": _make_agent("johnny"),
        "luna": _make_agent("luna"),
    }
    # Mark Johnny as orchestrator
    agents["johnny"].is_orchestrator = True

    registry = MagicMock()
    registry.get.side_effect = lambda n: agents.get(n)
    registry.get_orchestrator.return_value = agents["johnny"]
    registry.list_agents.return_value = list(agents.values())
    return registry, agents


@pytest.fixture
def orchestrator_spy():
    """Mock Orchestrator that records spawn_task calls and exposes _spawned_tasks."""
    orch = MagicMock()
    orch._spawned_tasks = {}
    orch.spawn_task = MagicMock(return_value=MagicMock(task_id="unused"))
    orch.hub = None
    return orch


@pytest.fixture
def api(orchestrator_spy, registry_with_agents, store) -> TaskAPI:
    registry, _ = registry_with_agents
    return TaskAPI(orchestrator=orchestrator_spy, registry=registry, store=store)


# -- submit_task -------------------------------------------------------


def test_submit_task_persists_row_and_spawns(api, orchestrator_spy, store):
    result = api.submit_task(TaskSubmission(prompt="Ship it", agent="albert"))
    assert result.status == "pending"
    assert result.agent == "albert"
    assert len(result.task_id) == 12  # uuid4 short form

    # Persisted to DB
    row = store.get_task(result.task_id)
    assert row is not None
    assert row["agent_name"] == "albert"
    assert row["prompt"] == "Ship it"
    assert row["status"] == "pending"
    assert row["platform"] == "api"

    # Orchestrator was asked to spawn with the same task_id
    orchestrator_spy.spawn_task.assert_called_once()
    kwargs = orchestrator_spy.spawn_task.call_args.kwargs
    assert kwargs["task_id"] == result.task_id
    assert kwargs["prompt"] == "Ship it"


def test_submit_task_empty_prompt_rejected(api):
    result = api.submit_task(TaskSubmission(prompt="   "))
    assert result.status == "rejected"
    assert "empty" in (result.error or "").lower()


def test_submit_task_unknown_agent_rejected(api):
    result = api.submit_task(TaskSubmission(prompt="hi", agent="ghost"))
    assert result.status == "rejected"
    assert "unknown agent" in (result.error or "").lower()


def test_submit_task_auto_routes_to_orchestrator_when_no_agent(api):
    """When no agent is specified, the orchestrator agent should be used."""
    result = api.submit_task(TaskSubmission(prompt="open question"))
    assert result.status == "pending"
    assert result.agent == "johnny"  # Marked as orchestrator in fixture


def test_submit_task_stores_metadata_as_json(api, store):
    result = api.submit_task(
        TaskSubmission(prompt="do thing", agent="albert", metadata={"source": "paperclip"}),
    )
    row = store.get_task(result.task_id)
    assert row["metadata"] == '{"source": "paperclip"}'


def test_submit_task_stores_goal_id(api, store):
    result = api.submit_task(
        TaskSubmission(prompt="do thing", agent="albert", goal_id=42),
    )
    row = store.get_task(result.task_id)
    assert row["goal_id"] == 42


# -- get_task / list_pending / list_recent -----------------------------


def test_get_task_returns_none_when_missing(api):
    assert api.get_task("nonexistent") is None


def test_get_task_merges_live_state(api, orchestrator_spy):
    result = api.submit_task(TaskSubmission(prompt="hi", agent="albert"))
    # Simulate a live spawned task
    spawned = MagicMock()
    spawned.status = "running"
    spawned.cost = 0.007
    orchestrator_spy._spawned_tasks[result.task_id] = spawned

    row = api.get_task(result.task_id)
    assert row is not None
    assert row["live_status"] == "running"
    assert row["live_cost"] == 0.007


def test_list_pending_filters_by_agent(api):
    api.submit_task(TaskSubmission(prompt="a", agent="albert"))
    api.submit_task(TaskSubmission(prompt="b", agent="johnny"))
    api.submit_task(TaskSubmission(prompt="c", agent="luna"))

    all_rows = api.list_pending()
    assert len(all_rows) == 3

    only_albert = api.list_pending(agent="albert")
    assert len(only_albert) == 1
    assert only_albert[0]["agent_name"] == "albert"


def test_list_recent_returns_all_submitted(api, store):
    ids = set()
    for i in range(3):
        r = api.submit_task(TaskSubmission(prompt=f"task {i}", agent="albert"))
        ids.add(r.task_id)

    recent = api.list_recent(limit=10)
    assert len(recent) == 3
    assert {row["id"] for row in recent} == ids


# -- cancel_task ------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_pending_task_updates_db(api, store):
    result = api.submit_task(TaskSubmission(prompt="slow task", agent="albert"))
    out = await api.cancel_task(result.task_id)
    assert out["status"] == "cancelled"

    row = store.get_task(result.task_id)
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_unknown_task_returns_unknown(api):
    out = await api.cancel_task("nonexistent")
    assert out["status"] == "unknown"
    assert out["cancelled"] is False


@pytest.mark.asyncio
async def test_cancel_running_task_cancels_future(api, orchestrator_spy):
    result = api.submit_task(TaskSubmission(prompt="running", agent="albert"))

    # Attach a real asyncio future so cancel() has effect
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    spawned = MagicMock()
    spawned.status = "running"
    spawned.cost = 0.0
    spawned._future = fut
    orchestrator_spy._spawned_tasks[result.task_id] = spawned

    out = await api.cancel_task(result.task_id)
    assert out["cancelled"] is True
    assert fut.cancelled()


# -- broadcast on cancel ----------------------------------------------


@pytest.mark.asyncio
async def test_cancel_invokes_hub_broadcast(api, orchestrator_spy):
    from unittest.mock import AsyncMock

    hub = MagicMock()
    hub.task_cancelled = AsyncMock()
    orchestrator_spy.hub = hub

    result = api.submit_task(TaskSubmission(prompt="x", agent="albert"))
    await api.cancel_task(result.task_id)

    hub.task_cancelled.assert_awaited_once()
    args = hub.task_cancelled.call_args.args
    assert args[0] == result.task_id
    assert args[1] == "albert"
