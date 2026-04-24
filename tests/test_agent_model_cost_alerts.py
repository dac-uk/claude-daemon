"""Tests for per-agent model daily cost alerts.

Covers:
- ConversationStore.get_daily_agent_model_costs() query
- SchedulerEngine._resolve_model_threshold() helper
- SchedulerEngine._job_agent_model_cost_alerts() alert firing logic
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_daemon.memory.store import ConversationStore
from claude_daemon.scheduler.engine import SchedulerEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


def _make_engine(store: ConversationStore, thresholds: dict | None = None) -> SchedulerEngine:
    """Build a minimal SchedulerEngine with mocked daemon and config."""
    if thresholds is None:
        thresholds = {"opus": 5.0, "default": 10.0}

    config = MagicMock()
    config.agent_model_cost_alert_usd = thresholds
    config.alert_webhook_urls = []
    config.alert_webhook_timeout = 5
    config.data_dir = Path("/tmp")

    daemon = MagicMock()
    daemon.store = store
    daemon.durable = None
    daemon.router = None

    engine = SchedulerEngine.__new__(SchedulerEngine)
    engine.config = config
    engine.daemon = daemon
    engine._loop = None
    engine._failure_counts = {}
    engine._max_failures = 3
    engine._agent_locks = {}
    return engine


# ---------------------------------------------------------------------------
# ConversationStore.get_daily_agent_model_costs
# ---------------------------------------------------------------------------

def test_get_daily_agent_model_costs_empty(store: ConversationStore):
    """Returns empty list when there are no metrics today."""
    assert store.get_daily_agent_model_costs() == []


def test_get_daily_agent_model_costs_aggregates(store: ConversationStore):
    """Sums cost_usd per (agent_name, model) for today's rows."""
    store.record_agent_metric(
        agent_name="johnny", metric_type="chat",
        cost_usd=3.0, model="claude-opus-4-5",
    )
    store.record_agent_metric(
        agent_name="johnny", metric_type="heartbeat",
        cost_usd=2.5, model="claude-opus-4-5",
    )
    store.record_agent_metric(
        agent_name="penny", metric_type="chat",
        cost_usd=0.8, model="claude-sonnet-4-5",
    )

    rows = store.get_daily_agent_model_costs()
    by_key = {(r["agent_name"], r["model"]): r["total_cost"] for r in rows}

    assert pytest.approx(by_key[("johnny", "claude-opus-4-5")], rel=1e-4) == 5.5
    assert pytest.approx(by_key[("penny", "claude-sonnet-4-5")], rel=1e-4) == 0.8


def test_get_daily_agent_model_costs_excludes_blank(store: ConversationStore):
    """Rows with empty agent_name or model are excluded."""
    store.record_agent_metric(
        agent_name="", metric_type="chat", cost_usd=9.99, model="claude-opus-4-5",
    )
    store.record_agent_metric(
        agent_name="johnny", metric_type="chat", cost_usd=9.99, model="",
    )
    assert store.get_daily_agent_model_costs() == []


# ---------------------------------------------------------------------------
# SchedulerEngine._resolve_model_threshold
# ---------------------------------------------------------------------------

def test_resolve_opus_threshold():
    thresholds = {"opus": 5.0, "default": 10.0}
    result = SchedulerEngine._resolve_model_threshold("claude-opus-4-5", thresholds)
    assert result == 5.0


def test_resolve_default_threshold_for_unknown_model():
    thresholds = {"opus": 5.0, "default": 10.0}
    result = SchedulerEngine._resolve_model_threshold("claude-sonnet-4-5", thresholds)
    assert result == 10.0


def test_resolve_no_default_returns_none():
    thresholds = {"opus": 5.0}
    result = SchedulerEngine._resolve_model_threshold("claude-sonnet-4-5", thresholds)
    assert result is None


def test_resolve_case_insensitive():
    thresholds = {"Opus": 3.0, "default": 10.0}
    result = SchedulerEngine._resolve_model_threshold("claude-OPUS-4", thresholds)
    assert result == 3.0


# ---------------------------------------------------------------------------
# SchedulerEngine._job_agent_model_cost_alerts — integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cost_alert_fires_when_over_threshold(store: ConversationStore):
    """Alert is sent when an agent's daily cost for a model exceeds the threshold."""
    # Seed enough opus cost to trigger the $5 threshold
    store.record_agent_metric(
        agent_name="johnny", metric_type="chat",
        cost_usd=6.20, model="claude-opus-4-5",
    )

    engine = _make_engine(store, thresholds={"opus": 5.0, "default": 10.0})

    sent_messages: list[str] = []

    def _fake_send_cost_alert(message: str, **kwargs):
        sent_messages.append(message)

    engine._send_cost_alert = _fake_send_cost_alert
    engine._send_webhook_alerts = AsyncMock()

    await engine._job_agent_model_cost_alerts()

    assert len(sent_messages) == 1
    assert "johnny" in sent_messages[0]
    assert "6.20" in sent_messages[0]
    assert "5.00" in sent_messages[0]
    assert "opus" in sent_messages[0].lower()


@pytest.mark.asyncio
async def test_no_alert_when_under_threshold(store: ConversationStore):
    """No alert is sent when spend is at or below threshold."""
    store.record_agent_metric(
        agent_name="johnny", metric_type="chat",
        cost_usd=4.99, model="claude-opus-4-5",
    )

    engine = _make_engine(store, thresholds={"opus": 5.0, "default": 10.0})
    sent_messages: list[str] = []
    engine._send_cost_alert = lambda msg, **kw: sent_messages.append(msg)
    engine._send_webhook_alerts = AsyncMock()

    await engine._job_agent_model_cost_alerts()

    assert sent_messages == []


@pytest.mark.asyncio
async def test_alert_fires_for_non_opus_model_via_default(store: ConversationStore):
    """Default threshold applies to models that don't match a specific key."""
    store.record_agent_metric(
        agent_name="penny", metric_type="chat",
        cost_usd=11.0, model="claude-sonnet-4-5",
    )

    engine = _make_engine(store, thresholds={"opus": 5.0, "default": 10.0})
    sent_messages: list[str] = []
    engine._send_cost_alert = lambda msg, **kw: sent_messages.append(msg)
    engine._send_webhook_alerts = AsyncMock()

    await engine._job_agent_model_cost_alerts()

    assert len(sent_messages) == 1
    assert "penny" in sent_messages[0]
    assert "sonnet" in sent_messages[0].lower()


@pytest.mark.asyncio
async def test_no_alert_when_no_default_and_unmatched_model(store: ConversationStore):
    """When 'default' is absent and model doesn't match any key, no alert fires."""
    store.record_agent_metric(
        agent_name="penny", metric_type="chat",
        cost_usd=99.0, model="claude-sonnet-4-5",
    )

    engine = _make_engine(store, thresholds={"opus": 5.0})  # no "default"
    sent_messages: list[str] = []
    engine._send_cost_alert = lambda msg, **kw: sent_messages.append(msg)
    engine._send_webhook_alerts = AsyncMock()

    await engine._job_agent_model_cost_alerts()

    assert sent_messages == []
