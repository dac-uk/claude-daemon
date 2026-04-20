"""Tests for GET /api/improvement/plan — reads the markdown file + archive."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_daemon.integrations.http_api import HttpApi
from claude_daemon.memory.store import ConversationStore


API_KEY = "plan-test-key"


def _daemon_with_store(store: ConversationStore, data_dir: Path) -> MagicMock:
    daemon = MagicMock()
    daemon.config = SimpleNamespace(
        api_bind="127.0.0.1",
        api_port=0,
        api_key=API_KEY,
        dashboard_enabled=True,
        github_webhook_secret="",
        stripe_webhook_secret="",
        data_dir=data_dir,
    )
    daemon.agent_registry = {}
    daemon.process_manager = SimpleNamespace(active_count=0)
    daemon.store = store
    daemon.orchestrator = None
    return daemon


@pytest.fixture
def store(tmp_path: Path):
    s = ConversationStore(tmp_path / "plan.db")
    yield s
    s.close()


@pytest.fixture
async def client(store, tmp_path):
    api = HttpApi(_daemon_with_store(store, tmp_path), port=0, api_key=API_KEY)
    server = TestServer(api._app)
    async with TestClient(server) as c:
        yield c


def _headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"}


async def test_plan_endpoint_missing_file_returns_empty(client):
    resp = await client.get("/api/improvement/plan", headers=_headers())
    assert resp.status == 200
    data = await resp.json()
    assert data["markdown"] == ""
    assert data["mtime"] is None
    assert data["archive"] == []
    assert data["truncated"] is False


async def test_plan_endpoint_returns_file_contents(client, tmp_path):
    playbooks = tmp_path / "shared" / "playbooks"
    playbooks.mkdir(parents=True)
    plan_body = "# Weekly improvement plan\n\n- Do good things.\n"
    (playbooks / "improvement-plan.md").write_text(plan_body)

    resp = await client.get("/api/improvement/plan", headers=_headers())
    assert resp.status == 200
    data = await resp.json()
    assert data["markdown"] == plan_body
    assert data["mtime"] is not None
    assert "T" in data["mtime"]  # ISO-8601
    assert data["truncated"] is False


async def test_plan_endpoint_truncates_large_file(client, tmp_path):
    playbooks = tmp_path / "shared" / "playbooks"
    playbooks.mkdir(parents=True)
    big = "x" * 70000
    (playbooks / "improvement-plan.md").write_text(big)

    resp = await client.get("/api/improvement/plan", headers=_headers())
    assert resp.status == 200
    data = await resp.json()
    assert len(data["markdown"]) == 65536
    assert data["truncated"] is True


async def test_plan_endpoint_lists_archive_newest_first(client, tmp_path):
    playbooks = tmp_path / "shared" / "playbooks"
    archive = playbooks / "archive"
    archive.mkdir(parents=True)
    (archive / "improvement-plan-2026-04-06.md").write_text("old")
    (archive / "improvement-plan-2026-04-13.md").write_text("mid")
    (archive / "improvement-plan-2026-04-20.md").write_text("new")

    resp = await client.get("/api/improvement/plan", headers=_headers())
    data = await resp.json()
    dates = [e["date"] for e in data["archive"]]
    assert dates == ["2026-04-20", "2026-04-13", "2026-04-06"]
