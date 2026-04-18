"""Phase 13 Part A/B regression tests — task origin tagging.

Covers the ``source`` column added to ``task_queue`` so the Operations
tab can surface chat-initiated work alongside spawn-dispatched and
API-submitted tasks.
"""

from __future__ import annotations

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


def _agent(name: str) -> MagicMock:
    a = MagicMock()
    a.name = name
    return a


@pytest.fixture
def api(store) -> TaskAPI:
    agents = {"albert": _agent("albert")}
    agents["albert"].is_orchestrator = True
    registry = MagicMock()
    registry.get.side_effect = lambda n: agents.get(n)
    registry.get_orchestrator.return_value = agents["albert"]
    registry.list_agents.return_value = list(agents.values())

    orch = MagicMock()
    orch._spawned_tasks = {}
    orch.spawn_task = MagicMock(return_value=MagicMock(task_id="unused"))
    orch.hub = None
    return TaskAPI(orchestrator=orch, registry=registry, store=store)


# -- schema migration -------------------------------------------------


def test_task_queue_has_source_column(store):
    """Migration creates the source column with a default of 'api'."""
    cols = {r[1] for r in store._db.execute("PRAGMA table_info(task_queue)").fetchall()}
    assert "source" in cols


# -- create_task accepts source --------------------------------------


def test_create_task_persists_source(store):
    store.create_task(
        task_id="abc123", agent_name="albert", prompt="hi",
        source="chat",
    )
    row = store.get_task("abc123")
    assert row["source"] == "chat"


def test_create_task_defaults_to_api(store):
    store.create_task(task_id="xyz999", agent_name="albert", prompt="hi")
    row = store.get_task("xyz999")
    assert row["source"] == "api"


# -- TaskAPI.submit_task defaults to source='api' ---------------------


def test_submit_task_defaults_to_api_source(api, store):
    result = api.submit_task(TaskSubmission(prompt="Run", agent="albert"))
    row = store.get_task(result.task_id)
    assert row["source"] == "api"


def test_submit_task_preserves_explicit_source(api, store):
    result = api.submit_task(
        TaskSubmission(prompt="Sync", agent="albert", source="heartbeat"),
    )
    row = store.get_task(result.task_id)
    assert row["source"] == "heartbeat"


# -- backward compatibility: pre-existing rows ------------------------


def test_pre_existing_rows_migrate_with_api_default(store):
    """An older row inserted before the migration would have NULL source
    on ALTER TABLE ADD COLUMN ... DEFAULT — SQLite backfills the default."""
    # Bypass create_task() to simulate a legacy INSERT that omitted source.
    store._db.execute(
        "INSERT INTO task_queue (id, agent_name, prompt, status) "
        "VALUES (?, ?, ?, ?)",
        ("legacy1", "albert", "old", "completed"),
    )
    store._db.commit()
    row = store.get_task("legacy1")
    assert row["source"] == "api"
