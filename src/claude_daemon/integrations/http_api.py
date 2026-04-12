"""HTTP REST API integration for claude-daemon.

Exposes the daemon's agent system as a REST API for external automation,
webhooks, and programmatic access. Includes WebSocket event bus and live
agent dashboard when dashboard_enabled is set.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from claude_daemon.integrations.dashboard import DashboardHub

if TYPE_CHECKING:
    from claude_daemon.core.daemon import ClaudeDaemon

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent.parent / "static"


class HttpApi:
    """Lightweight REST API for the daemon with optional live dashboard."""

    def __init__(self, daemon: ClaudeDaemon, port: int, api_key: str = "") -> None:
        self.daemon = daemon
        self.port = port
        self.api_key = api_key
        self.hub = DashboardHub()
        self._app = web.Application(middlewares=[self._auth_middleware])
        self._runner: web.AppRunner | None = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        self._app.router.add_get("/api/health", self._handle_health)
        self._app.router.add_get("/api/agents", self._handle_agents)
        self._app.router.add_get("/api/status", self._handle_status)
        self._app.router.add_get("/api/sessions", self._handle_sessions)
        self._app.router.add_get("/api/tasks", self._handle_tasks)
        self._app.router.add_post("/api/message", self._handle_message)
        self._app.router.add_post("/api/workflow", self._handle_workflow)
        self._app.router.add_get("/api/metrics", self._handle_metrics)
        self._app.router.add_post("/api/webhook/{source}", self._handle_webhook)

        # Dashboard: WebSocket + static serving
        if self.daemon.config.dashboard_enabled:
            self._app.router.add_get("/ws", self._handle_ws)
            self._app.router.add_get("/", self._handle_dashboard)
            log.info("Dashboard enabled — serving at http://localhost:%d/", self.port)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # Public endpoints: health check and dashboard static
        if request.path in ("/api/health", "/"):
            return await handler(request)

        if self.api_key:
            # WebSocket auth via query param or header
            auth = request.headers.get("Authorization", "")
            query_key = request.query.get("key", "")
            token = auth[7:] if auth.startswith("Bearer ") else query_key
            if token != self.api_key:
                return web.json_response(
                    {"error": "Unauthorized"}, status=401,
                )

        return await handler(request)

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        bind = self.daemon.config.api_bind
        site = web.TCPSite(self._runner, bind, self.port)
        await site.start()
        log.info("HTTP API started on %s:%d", bind, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            log.info("HTTP API stopped")

    # -- Handlers --

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "agents": len(self.daemon.agent_registry) if self.daemon.agent_registry else 0,
            "active_sessions": (
                self.daemon.process_manager.active_count
                if self.daemon.process_manager else 0
            ),
        })

    async def _handle_agents(self, request: web.Request) -> web.Response:
        if not self.daemon.agent_registry:
            return web.json_response({"agents": []})

        agents = []
        for agent in self.daemon.agent_registry:
            agents.append({
                "name": agent.name,
                "role": agent.identity.role,
                "emoji": agent.identity.emoji,
                "model": agent.identity.default_model,
                "is_orchestrator": agent.is_orchestrator,
                "has_mcp": agent.mcp_config_path is not None,
                "heartbeat_tasks": len(agent.parse_heartbeat_tasks()),
            })
        return web.json_response({"agents": agents})

    async def _handle_status(self, request: web.Request) -> web.Response:
        stats = self.daemon.store.get_stats() if self.daemon.store else {}
        return web.json_response({
            "status": "running",
            "agents": len(self.daemon.agent_registry) if self.daemon.agent_registry else 0,
            "active_sessions": (
                self.daemon.process_manager.active_count
                if self.daemon.process_manager else 0
            ),
            "total_sessions": stats.get("total", 0),
            "total_messages": stats.get("total_messages", 0),
            "total_cost": stats.get("total_cost", 0),
        })

    async def _handle_message(self, request: web.Request) -> web.Response:
        """Send a message to an agent.

        POST /api/message
        {
            "message": "...",
            "agent": "albert",       // optional, defaults to orchestrator
            "user_id": "api-user",   // optional
        }
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        message = body.get("message", "").strip()
        if not message:
            return web.json_response({"error": "Missing 'message' field"}, status=400)

        agent_name = body.get("agent")
        user_id = body.get("user_id", "api-user")

        try:
            result = await self.daemon.handle_message(
                prompt=message,
                platform="api",
                user_id=user_id,
                agent_name=agent_name,
            )
            return web.json_response({"result": result})
        except Exception as e:
            log.exception("API message handler error")
            return web.json_response(
                {"error": f"Internal error: {e}"}, status=500,
            )

    async def _handle_workflow(self, request: web.Request) -> web.Response:
        """Run the build quality gate workflow.

        POST /api/workflow
        {"request": "build a settings page"}
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        request_text = body.get("request", "").strip()
        if not request_text:
            return web.json_response({"error": "Missing 'request' field"}, status=400)

        try:
            result = await self.daemon.run_build_workflow(request_text)
            return web.json_response({"result": result})
        except Exception as e:
            log.exception("Workflow API error")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Get per-agent metrics.

        GET /api/metrics?agent=albert&days=7
        """
        agent = request.query.get("agent")
        days = int(request.query.get("days", "7"))

        if not self.daemon.store:
            return web.json_response({"metrics": []})

        metrics = self.daemon.store.get_agent_metrics(agent_name=agent, days=days)
        return web.json_response({"metrics": metrics})

    async def _handle_sessions(self, request: web.Request) -> web.Response:
        """GET /api/sessions — active Claude subprocesses."""
        if not self.daemon.process_manager:
            return web.json_response({"sessions": []})
        sessions = []
        for sid, active in self.daemon.process_manager._active.items():
            sessions.append({
                "session_id": sid[:12],
                "platform": active.platform,
                "user_id": active.user_id,
                "started_at": active.started_at.isoformat(),
            })
        return web.json_response({"sessions": sessions})

    async def _handle_tasks(self, request: web.Request) -> web.Response:
        """GET /api/tasks — spawned background tasks."""
        if not self.daemon.orchestrator:
            return web.json_response({"tasks": []})
        tasks = []
        for t in self.daemon.orchestrator.list_tasks():
            tasks.append({
                "task_id": t.task_id,
                "agent": t.agent_name,
                "status": t.status,
                "prompt": t.prompt[:200],
                "cost": t.cost,
            })
        return web.json_response({"tasks": tasks})

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint for live dashboard events."""
        return await self.hub.ws_handler(request)

    async def _handle_dashboard(self, request: web.Request) -> web.Response:
        """Serve the dashboard HTML."""
        html_path = STATIC_DIR / "dashboard.html"
        if not html_path.exists():
            return web.Response(text="Dashboard not found", status=404)
        return web.FileResponse(html_path)

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Receive external webhooks and route to appropriate agent.

        POST /api/webhook/{source}

        Supported sources: github, stripe, generic
        """
        source = request.match_info["source"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            body = {}

        # Route based on source
        agent_name = None
        prompt = ""

        if source == "github":
            event_type = request.headers.get("X-GitHub-Event", "unknown")
            prompt = (
                f"[GitHub webhook: {event_type}]\n\n"
                f"{json.dumps(body, indent=2)[:3000]}"
            )
            # PR events go to Max for review, push events go to Albert
            if event_type in ("pull_request", "pull_request_review"):
                agent_name = "max"
            elif event_type == "push":
                agent_name = "albert"
            elif event_type in ("issues", "issue_comment"):
                agent_name = "johnny"
            else:
                agent_name = "johnny"

        elif source == "stripe":
            event_type = body.get("type", "unknown")
            prompt = (
                f"[Stripe webhook: {event_type}]\n\n"
                f"{json.dumps(body, indent=2)[:3000]}"
            )
            agent_name = "penny"

        else:
            # Generic webhook — route to Johnny (orchestrator)
            prompt = (
                f"[Webhook from {source}]\n\n"
                f"{json.dumps(body, indent=2)[:3000]}"
            )
            agent_name = "johnny"

        if prompt:
            try:
                result = await self.daemon.handle_message(
                    prompt=prompt,
                    platform=f"webhook:{source}",
                    user_id=f"webhook:{source}",
                    agent_name=agent_name,
                )
                return web.json_response({
                    "status": "processed",
                    "agent": agent_name,
                    "result": result[:5000] if result else "",
                })
            except Exception as e:
                log.exception("Webhook handler error")
                return web.json_response(
                    {"error": str(e)}, status=500,
                )

        return web.json_response({"status": "ignored"})
