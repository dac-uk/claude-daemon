"""Regression tests for /api/daemon/status and /api/status diagnostics.

Phase 13 plan, Part C — when the user opened Daemon Control they saw
``PID: ?  Uptime: ?  Version: ?  Host: ?`` because no test existed to
catch a silent regression on this endpoint. These tests cover:

    - /api/daemon/status returns pid (int), version (non-empty), host (str),
      uptime_seconds (None if started_at unset, else positive float)
    - /api/daemon/status requires auth (like every other /api/* route)
    - /api/status includes the new ``version`` + ``pid`` fallback fields
      so even a stale daemon scenario keeps the dashboard topbar populated.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_daemon.integrations.http_api import HttpApi


API_KEY = "test-key"


def _make_daemon(started_at: float | None = None) -> MagicMock:
    daemon = MagicMock()
    daemon.config = SimpleNamespace(
        api_bind="127.0.0.1",
        api_port=0,
        api_key=API_KEY,
        dashboard_enabled=True,
        github_webhook_secret="",
        stripe_webhook_secret="",
    )
    daemon.agent_registry = []
    daemon.process_manager = SimpleNamespace(active_count=0)
    daemon.store = None
    daemon.orchestrator = None
    daemon.router = None
    daemon.started_at = started_at
    return daemon


@pytest.fixture
async def client_running():
    """Client for an api instance with a ``started_at`` set — simulating a
    daemon that has been running for a few moments."""
    api = HttpApi(_make_daemon(started_at=time.time() - 5), port=0, api_key=API_KEY)
    server = TestServer(api._app)
    async with TestClient(server) as c:
        yield c


@pytest.fixture
async def client_fresh():
    """Client where started_at is None (boot state)."""
    api = HttpApi(_make_daemon(started_at=None), port=0, api_key=API_KEY)
    server = TestServer(api._app)
    async with TestClient(server) as c:
        yield c


def _auth():
    return {"Authorization": f"Bearer {API_KEY}"}


# -- /api/daemon/status -----------------------------------------------


async def test_daemon_status_returns_pid_and_version(client_running):
    resp = await client_running.get("/api/daemon/status", headers=_auth())
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body["pid"], int) and body["pid"] > 0
    assert isinstance(body["version"], str) and body["version"]
    assert isinstance(body["host"], str) and body["host"]
    # uptime should be a positive float given started_at is 5s ago
    assert body["uptime_seconds"] is not None
    assert body["uptime_seconds"] >= 4.0


async def test_daemon_status_uptime_none_when_started_at_unset(client_fresh):
    resp = await client_fresh.get("/api/daemon/status", headers=_auth())
    assert resp.status == 200
    body = await resp.json()
    assert body["uptime_seconds"] is None


async def test_daemon_status_requires_auth(client_running):
    """No credentials → 401; guards against an accidental public-endpoint regression."""
    resp = await client_running.get("/api/daemon/status")
    assert resp.status == 401


# -- /api/status fallback diagnostics ---------------------------------


async def test_status_includes_version_and_pid(client_running):
    """These fallback fields let the dashboard show version/pid even when
    /api/daemon/status is unreachable (stale daemon / endpoint errored)."""
    resp = await client_running.get("/api/status", headers=_auth())
    assert resp.status == 200
    body = await resp.json()
    assert "version" in body
    assert "pid" in body
    assert isinstance(body["pid"], int) and body["pid"] > 0
    assert isinstance(body["version"], str) and body["version"]
