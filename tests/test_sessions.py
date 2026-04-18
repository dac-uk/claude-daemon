"""Regression tests for ConversationStore.list_sessions + session_summary.

These back the "Sessions" topbar stat and drill-down modal in the
Command Center — total includes chatted + spawned; each agent's chat and
spawn counts are surfaced separately so the user can click through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_daemon.memory.store import ConversationStore


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


def _seed_conversation(
    s: ConversationStore, user_id: str, platform: str = "api",
) -> str:
    conv = s.get_or_create_conversation(
        session_id=None, platform=platform, user_id=user_id,
    )
    return conv["session_id"]


def test_list_sessions_empty_store(store: ConversationStore) -> None:
    assert store.list_sessions() == []
    summary = store.session_summary()
    assert summary["total"] == 0
    assert summary["by_agent"] == {}
    assert summary["unattributed"] == 0


def test_attribute_session_chat_format() -> None:
    agent, tid = ConversationStore._attribute_session("dashboard:albert")
    assert agent == "albert"
    assert tid is None


def test_attribute_session_spawn_format() -> None:
    agent, tid = ConversationStore._attribute_session("dashboard:spawn:task_abc")
    assert agent is None
    assert tid == "task_abc"


def test_attribute_session_legacy_no_colon() -> None:
    agent, tid = ConversationStore._attribute_session("legacy")
    assert agent is None
    assert tid is None


def test_list_sessions_chat_attributed_to_agent(store: ConversationStore) -> None:
    _seed_conversation(store, "dashboard:albert")
    _seed_conversation(store, "dashboard:jeremy")
    sessions = store.list_sessions()
    agents = sorted(s["agent"] for s in sessions if s["agent"])
    assert agents == ["albert", "jeremy"]
    assert all(s["kind"] == "chat" for s in sessions)


def test_list_sessions_spawn_resolved_via_task_queue(
    store: ConversationStore,
) -> None:
    store.create_task(
        task_id="task_xyz", agent_name="luna", prompt="draw a horse",
    )
    _seed_conversation(store, "dashboard:spawn:task_xyz")
    sessions = store.list_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["agent"] == "luna"
    assert s["task_id"] == "task_xyz"
    assert s["kind"] == "spawn"


def test_list_sessions_spawn_with_missing_task_is_unattributed(
    store: ConversationStore,
) -> None:
    _seed_conversation(store, "dashboard:spawn:missing_task")
    sessions = store.list_sessions()
    assert sessions[0]["agent"] is None
    assert sessions[0]["task_id"] == "missing_task"


def test_session_summary_counts_chat_and_spawn_separately(
    store: ConversationStore,
) -> None:
    store.create_task(task_id="t1", agent_name="albert", prompt="p1")
    store.create_task(task_id="t2", agent_name="albert", prompt="p2")
    _seed_conversation(store, "dashboard:albert")
    _seed_conversation(store, "dashboard:spawn:t1")
    _seed_conversation(store, "dashboard:spawn:t2")
    _seed_conversation(store, "dashboard:jeremy")
    summary = store.session_summary()
    assert summary["total"] == 4
    assert summary["by_agent"]["albert"] == {"chat": 1, "spawn": 2, "total": 3}
    assert summary["by_agent"]["jeremy"] == {"chat": 1, "spawn": 0, "total": 1}
    assert summary["unattributed"] == 0


def test_session_summary_counts_unattributed(store: ConversationStore) -> None:
    _seed_conversation(store, "legacy-no-colon")
    _seed_conversation(store, "dashboard:spawn:missing_task")
    summary = store.session_summary()
    assert summary["unattributed"] == 2
    assert summary["by_agent"] == {}
