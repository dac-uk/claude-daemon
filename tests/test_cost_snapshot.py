"""Regression tests for ConversationStore.get_cost_snapshot (Phase 11B).

The dashboard displays cost in three places (topbar, Agent Fleet cards,
Cost tab). Before this helper each site read a different source and
produced a different number. These tests pin the reconciled view.
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
    s: ConversationStore, user_id: str, cost: float, platform: str = "api",
) -> str:
    conv = s.get_or_create_conversation(
        session_id=None, platform=platform, user_id=user_id,
    )
    s.update_conversation(conv["id"], cost=cost)
    return conv["session_id"]


def _seed_metric(
    s: ConversationStore, agent_name: str, cost: float,
    metric_type: str = "heartbeat",
) -> None:
    s.record_agent_metric(
        agent_name=agent_name, metric_type=metric_type,
        cost_usd=cost, model="sonnet", platform="heartbeat", success=True,
    )


def test_cost_snapshot_empty_store_returns_zeros(store: ConversationStore) -> None:
    snap = store.get_cost_snapshot()
    assert snap["total_usd"] == 0
    assert snap["by_agent"] == {}
    assert snap["by_source"]["conversations"] == 0
    assert snap["by_source"]["agent_metrics"] == 0


def test_cost_snapshot_agent_metrics_authoritative_for_by_agent(
    store: ConversationStore,
) -> None:
    _seed_metric(store, "albert", 0.15)
    _seed_metric(store, "albert", 0.05)
    _seed_metric(store, "jeremy", 0.20)
    snap = store.get_cost_snapshot()
    assert snap["by_agent"]["albert"] == pytest.approx(0.20)
    assert snap["by_agent"]["jeremy"] == pytest.approx(0.20)


def test_cost_snapshot_skips_spawn_user_id_format(
    store: ConversationStore,
) -> None:
    """user_id="<user>:spawn:<task_id>" must not be attributed to task_id."""
    _seed_conversation(store, "dashboard:spawn:task_abc123", 0.50)
    snap = store.get_cost_snapshot()
    assert "task_abc123" not in snap["by_agent"]


def test_cost_snapshot_folds_in_chat_only_agent(
    store: ConversationStore,
) -> None:
    """An agent with conversations but no metrics row still shows up."""
    _seed_conversation(store, "dashboard:luna", 0.03)
    snap = store.get_cost_snapshot()
    assert snap["by_agent"].get("luna") == pytest.approx(0.03)


def test_cost_snapshot_metrics_wins_when_both_sources_populated(
    store: ConversationStore,
) -> None:
    _seed_conversation(store, "dashboard:albert", 1.00)
    _seed_metric(store, "albert", 2.00)
    snap = store.get_cost_snapshot()
    # agent_metrics wins for by_agent (more complete).
    assert snap["by_agent"]["albert"] == pytest.approx(2.00)


def test_cost_snapshot_total_is_max_of_two_sources(
    store: ConversationStore,
) -> None:
    _seed_conversation(store, "dashboard:albert", 1.79)
    _seed_metric(store, "albert", 2.07)
    snap = store.get_cost_snapshot()
    assert snap["total_usd"] == pytest.approx(2.07)
    assert snap["by_source"]["conversations"] == pytest.approx(1.79)
    assert snap["by_source"]["agent_metrics"] == pytest.approx(2.07)
    assert snap["by_source"]["deduped_total"] == pytest.approx(2.07)


def test_count_agents_with_conversations_excludes_spawn(
    store: ConversationStore,
) -> None:
    _seed_conversation(store, "dashboard:albert", 0.01)
    _seed_conversation(store, "dashboard:luna", 0.01)
    _seed_conversation(store, "dashboard:spawn:task_x", 0.01)
    _seed_conversation(store, "legacy-no-colon", 0.01)
    n = store.count_agents_with_conversations()
    # albert + luna = 2 — spawn and no-colon excluded.
    assert n == 2
