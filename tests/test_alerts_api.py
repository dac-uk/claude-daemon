"""Tests for the Alerts aggregator: /api/alerts, /api/logs/tail, alert_count."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_daemon.integrations.http_api import HttpApi
from claude_daemon.memory.store import ConversationStore


API_KEY = "alerts-test-key"


def _daemon_with_store(store: ConversationStore) -> MagicMock:
    daemon = MagicMock()
    daemon.config = SimpleNamespace(
        api_bind="127.0.0.1",
        api_port=0,
        api_key=API_KEY,
        dashboard_enabled=True,
        github_webhook_secret="",
        stripe_webhook_secret="",
    )
    daemon.agent_registry = {}
    daemon.process_manager = SimpleNamespace(active_count=0)
    daemon.store = store
    daemon.orchestrator = None
    return daemon


@pytest.fixture
def store(tmp_path: Path):
    s = ConversationStore(tmp_path / "alerts.db")
    yield s
    s.close()


@pytest.fixture
async def client(store, monkeypatch, tmp_path):
    # Redirect log_dir so daemon_log source never reads the real user log.
    from claude_daemon.utils import logs as logs_mod
    monkeypatch.setattr(logs_mod, "default_log_path", lambda: tmp_path / "daemon.log")

    api = HttpApi(_daemon_with_store(store), port=0, api_key=API_KEY)
    server = TestServer(api._app)
    async with TestClient(server) as c:
        yield c


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"}


async def test_alerts_empty_when_no_data(client):
    resp = await client.get("/api/alerts", headers=_headers())
    assert resp.status == 200
    body = await resp.json()
    assert body == {"alerts": []}


async def test_alerts_includes_failed_task(client, store):
    store.create_task("task-1", "albert", "prompt", task_type="default")
    store.update_task_status("task-1", "failed", error="kaboom")

    resp = await client.get("/api/alerts", headers=_headers())
    assert resp.status == 200
    alerts = (await resp.json())["alerts"]
    assert any(a["id"] == "task-task-1" for a in alerts)
    t = next(a for a in alerts if a["id"] == "task-task-1")
    assert t["severity"] == "error"
    assert t["agent"] == "albert"
    assert "kaboom" in t["message"]


async def test_alerts_marks_orphan_task_critical(client, store):
    store.create_task("task-orph", "luna", "prompt")
    store.update_task_status(
        "task-orph", "failed", error="daemon restarted — orphan task",
    )
    resp = await client.get("/api/alerts", headers=_headers())
    alerts = (await resp.json())["alerts"]
    t = next(a for a in alerts if a["entity_id"] == "task-orph")
    assert t["severity"] == "critical"
    assert t["source"] == "orphan_task"


async def test_alerts_severity_filter(client, store):
    store.create_task("t-err", "albert", "p")
    store.update_task_status("t-err", "failed", error="fail")

    resp = await client.get("/api/alerts?severity=error", headers=_headers())
    alerts = (await resp.json())["alerts"]
    assert all(a["severity"] == "error" for a in alerts)
    assert len(alerts) == 1


async def test_alerts_budget_over_80_pct_is_warning(client, store):
    from claude_daemon.orchestration.budgets import BudgetStore

    bs = BudgetStore(store)
    bid = bs.create(scope="global", limit_usd=1.00, period="daily")
    # Simulate 85% spend via direct SQL (no public API for spend-set).
    store._db.execute("UPDATE budgets SET current_spend=? WHERE id=?", (0.85, bid))
    store._db.commit()

    resp = await client.get("/api/alerts", headers=_headers())
    alerts = (await resp.json())["alerts"]
    budget_alert = next(a for a in alerts if a["id"] == f"budget-{bid}")
    assert budget_alert["severity"] == "warning"


async def test_alerts_budget_exhausted_is_critical(client, store):
    from claude_daemon.orchestration.budgets import BudgetStore

    bs = BudgetStore(store)
    bid = bs.create(scope="global", limit_usd=1.00, period="daily")
    store._db.execute("UPDATE budgets SET current_spend=? WHERE id=?", (1.20, bid))
    store._db.commit()

    resp = await client.get("/api/alerts", headers=_headers())
    alerts = (await resp.json())["alerts"]
    budget_alert = next(a for a in alerts if a["id"] == f"budget-{bid}")
    assert budget_alert["severity"] == "critical"


async def test_alerts_sorted_by_severity(client, store):
    from claude_daemon.orchestration.budgets import BudgetStore

    store.create_task("t-err", "a", "p")
    store.update_task_status("t-err", "failed", error="fail")

    bs = BudgetStore(store)
    bid = bs.create(scope="global", limit_usd=1.00, period="daily")
    store._db.execute("UPDATE budgets SET current_spend=? WHERE id=?", (1.20, bid))
    store._db.commit()

    alerts = (await (await client.get("/api/alerts", headers=_headers())).json())["alerts"]
    severities = [a["severity"] for a in alerts]
    # critical must come before error (budget_exhausted before failed_task)
    first_error = severities.index("error")
    first_critical = severities.index("critical")
    assert first_critical < first_error


async def test_alerts_pending_approval_has_approve_reject_actions(client, store):
    from claude_daemon.orchestration.approvals import ApprovalsStore

    appr_store = ApprovalsStore(store)
    store.create_task("t-pa", "albert", "p", initial_status="pending_approval")
    aid = appr_store.create(task_id="t-pa", reason="threshold", threshold_usd=0.50)

    alerts = (await (await client.get("/api/alerts", headers=_headers())).json())["alerts"]
    approval = next(a for a in alerts if a["id"] == f"approval-{aid}")
    assert approval["severity"] == "warning"
    labels = [act["label"] for act in approval["actions"]]
    assert "Approve" in labels and "Reject" in labels


async def test_status_endpoint_includes_alert_count(client, store):
    store.create_task("t-err", "albert", "p")
    store.update_task_status("t-err", "failed", error="boom")

    resp = await client.get("/api/status", headers=_headers())
    assert resp.status == 200
    body = await resp.json()
    assert "alert_count" in body
    assert body["alert_count"] >= 1


async def test_logs_tail_endpoint(client, tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text(
        "2026-04-18 12:00:00 [WARNING] claude_daemon.foo: hi\n"
        "2026-04-18 12:00:01 [ERROR] claude_daemon.foo: worse\n",
    )
    resp = await client.get("/api/logs/tail?lines=10", headers=_headers())
    assert resp.status == 200
    body = await resp.json()
    assert len(body["lines"]) == 2
    assert body["lines"][1]["level"] == "ERROR"


async def test_logs_tail_missing_file_returns_empty(client, tmp_path):
    # tmp_path has no daemon.log
    resp = await client.get("/api/logs/tail", headers=_headers())
    assert resp.status == 200
    body = await resp.json()
    assert body["lines"] == []
