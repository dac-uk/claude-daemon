"""Tests for Phase 7 dashboard authentication.

Covers the four auth paths the _auth_middleware supports:
    - cookie (cd_session) set by the login flow
    - Authorization: Bearer (unchanged, for external API callers)
    - Sec-WebSocket-Protocol (unchanged, for CLI websocket clients)
    - no-auth mode when api_key is empty
plus the /dashboard/login handler and /?key=<…> one-shot flow.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_daemon.integrations.http_api import HttpApi


API_KEY = "secret-key-12345"


def _make_daemon(api_key: str) -> MagicMock:
    """Minimal ClaudeDaemon stub — just enough surface for HttpApi."""
    daemon = MagicMock()
    daemon.config = SimpleNamespace(
        api_bind="127.0.0.1",
        api_port=0,
        api_key=api_key,
        dashboard_enabled=True,
        github_webhook_secret="",
        stripe_webhook_secret="",
    )
    # Agents + status endpoints read these directly.
    daemon.agent_registry = []
    daemon.process_manager = SimpleNamespace(active_count=0)
    daemon.store = None
    daemon.orchestrator = None
    return daemon


@pytest.fixture
async def client_with_key():
    """aiohttp TestClient for an HttpApi instance that requires auth."""
    api = HttpApi(_make_daemon(API_KEY), port=0, api_key=API_KEY)
    server = TestServer(api._app)
    async with TestClient(server) as c:
        yield c


@pytest.fixture
async def client_no_auth():
    """aiohttp TestClient for an HttpApi instance with no api_key (open)."""
    api = HttpApi(_make_daemon(""), port=0, api_key="")
    server = TestServer(api._app)
    async with TestClient(server) as c:
        yield c


# -- middleware -------------------------------------------------------


async def test_health_is_public_without_auth(client_with_key):
    resp = await client_with_key.get("/api/health")
    assert resp.status == 200


async def test_protected_route_returns_401_without_credentials(client_with_key):
    resp = await client_with_key.get("/api/agents")
    assert resp.status == 401


async def test_bearer_token_authorises_protected_route(client_with_key):
    resp = await client_with_key.get(
        "/api/agents",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert "agents" in body


async def test_wrong_bearer_token_returns_401(client_with_key):
    resp = await client_with_key.get(
        "/api/agents",
        headers={"Authorization": "Bearer not-the-key"},
    )
    assert resp.status == 401


async def test_cookie_authorises_protected_route(client_with_key):
    client_with_key.session.cookie_jar.update_cookies({"cd_session": API_KEY})
    resp = await client_with_key.get("/api/agents")
    assert resp.status == 200


async def test_wrong_cookie_returns_401(client_with_key):
    client_with_key.session.cookie_jar.update_cookies({"cd_session": "wrong"})
    resp = await client_with_key.get("/api/agents")
    assert resp.status == 401


async def test_ws_protocol_header_authorises(client_with_key):
    """CLI websocket clients carry the token in Sec-WebSocket-Protocol."""
    resp = await client_with_key.get(
        "/api/agents",
        headers={"Sec-WebSocket-Protocol": API_KEY},
    )
    assert resp.status == 200


async def test_no_auth_mode_lets_protected_route_through(client_no_auth):
    """When api_key is empty, no credentials are required."""
    resp = await client_no_auth.get("/api/agents")
    assert resp.status == 200


# -- /dashboard/login POST handler ------------------------------------


async def test_login_with_correct_key_sets_cookie_and_redirects(client_with_key):
    resp = await client_with_key.post(
        "/dashboard/login",
        data={"key": API_KEY},
        allow_redirects=False,
    )
    assert resp.status == 302
    assert resp.headers["Location"] == "/"
    cookie = resp.cookies.get("cd_session")
    assert cookie is not None
    assert cookie.value == API_KEY
    # Cookie must be httponly for XSS protection.
    assert cookie["httponly"]


async def test_login_with_wrong_key_returns_403(client_with_key):
    resp = await client_with_key.post(
        "/dashboard/login",
        data={"key": "nope"},
        allow_redirects=False,
    )
    assert resp.status == 403
    assert resp.cookies.get("cd_session") is None


async def test_login_with_empty_body_returns_403(client_with_key):
    resp = await client_with_key.post(
        "/dashboard/login",
        data={},
        allow_redirects=False,
    )
    assert resp.status == 403


async def test_login_in_no_auth_mode_just_redirects(client_no_auth):
    """With no api_key, POST /dashboard/login is a no-op redirect."""
    resp = await client_no_auth.post(
        "/dashboard/login",
        data={"key": "irrelevant"},
        allow_redirects=False,
    )
    assert resp.status == 302
    assert resp.headers["Location"] == "/"


# -- / (dashboard) with query param and cookie -----------------------


async def test_root_with_correct_query_key_redirects_and_sets_cookie(client_with_key):
    resp = await client_with_key.get(
        "/?key=" + API_KEY,
        allow_redirects=False,
    )
    assert resp.status == 302
    assert resp.headers["Location"] == "/"
    cookie = resp.cookies.get("cd_session")
    assert cookie is not None
    assert cookie.value == API_KEY


async def test_root_with_wrong_query_key_returns_403(client_with_key):
    resp = await client_with_key.get(
        "/?key=wrong",
        allow_redirects=False,
    )
    assert resp.status == 403


async def test_root_without_credentials_serves_login_page(client_with_key):
    resp = await client_with_key.get("/", allow_redirects=False)
    # Login page returned with 401 so the browser knows not to cache a
    # "success" response — but the body is the login HTML form.
    assert resp.status == 401
    body = await resp.text()
    assert "CLAUDE COMMAND CENTER" in body
    assert 'name="key"' in body


async def test_root_with_valid_cookie_serves_dashboard(client_with_key):
    client_with_key.session.cookie_jar.update_cookies({"cd_session": API_KEY})
    resp = await client_with_key.get("/", allow_redirects=False)
    assert resp.status == 200
    body = await resp.text()
    # The dashboard shell references CC (the Command Center JS namespace).
    assert "<html" in body.lower()


async def test_root_in_no_auth_mode_serves_dashboard_directly(client_no_auth):
    resp = await client_no_auth.get("/", allow_redirects=False)
    assert resp.status == 200


# -- static assets remain public -------------------------------------


async def test_static_assets_are_public(client_with_key):
    resp = await client_with_key.get("/static/style.css")
    # style.css exists in the project; confirm it's reachable without auth.
    assert resp.status in (200, 404)  # 404 if file missing, still not 401
    assert resp.status != 401


# -- webhook path stays unauthenticated-by-header (signature-based) --


async def test_webhook_path_is_not_bearer_gated(client_with_key):
    """Webhook paths do their own signature check; middleware must skip bearer."""
    # Empty body + missing signature → handler returns 400/403/etc, NOT 401.
    resp = await client_with_key.post("/api/webhook/github", data=b"{}")
    assert resp.status != 401
