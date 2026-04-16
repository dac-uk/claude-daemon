"""HTTP REST API integration for claude-daemon.

Exposes the daemon's agent system as a REST API for external automation,
webhooks, and programmatic access. Includes WebSocket event bus and live
agent dashboard when dashboard_enabled is set.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
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
        self._app.router.add_post("/api/message/stream", self._handle_message_stream)
        self._app.router.add_post("/api/workflow", self._handle_workflow)
        self._app.router.add_get("/api/metrics", self._handle_metrics)
        self._app.router.add_post("/api/webhook/{source}", self._handle_webhook)
        self._app.router.add_get("/api/audit", self._handle_audit)
        self._app.router.add_get("/api/config/env", self._handle_env_list)
        self._app.router.add_post("/api/config/env", self._handle_env_set)
        self._app.router.add_get("/api/mcp", self._handle_mcp_list)
        self._app.router.add_post("/api/mcp/enable", self._handle_mcp_enable)
        self._app.router.add_post("/api/mcp/disable", self._handle_mcp_disable)
        self._app.router.add_post("/api/mcp/refresh", self._handle_mcp_refresh)
        self._app.router.add_post("/api/settings/thinking", self._handle_settings_thinking)
        self._app.router.add_post("/api/settings/effort", self._handle_settings_effort)
        self._app.router.add_post("/api/paperclip/heartbeat", self._handle_paperclip_heartbeat)
        self._app.router.add_get("/api/settings/backend", self._handle_backend_status)
        self._app.router.add_post("/api/settings/backend", self._handle_backend_set)
        self._app.router.add_get("/api/discussions", self._handle_discussions)
        self._app.router.add_get("/api/failures", self._handle_failures)
        self._app.router.add_get("/api/evolution", self._handle_evolution)

        # Dashboard: WebSocket + static serving
        if self.daemon.config.dashboard_enabled:
            self._app.router.add_get("/ws", self._handle_ws)
            self._app.router.add_static("/static", STATIC_DIR, show_index=False)
            self._app.router.add_get("/", self._handle_dashboard)
            log.info("Dashboard enabled — serving at http://localhost:%d/", self.port)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # Public endpoints: health check, dashboard, and static assets
        if request.path in ("/api/health", "/") or request.path.startswith("/static/"):
            return await handler(request)

        # Webhooks use their own signature verification, not bearer tokens
        if request.path.startswith("/api/webhook/"):
            return await handler(request)

        if self.api_key:
            auth = request.headers.get("Authorization", "")
            # WebSocket: accept token via Sec-WebSocket-Protocol header
            ws_proto = request.headers.get("Sec-WebSocket-Protocol", "")
            token = ""
            if auth.startswith("Bearer "):
                token = auth[7:]
            elif ws_proto:
                token = ws_proto
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

    # -- Webhook signature verification --

    def _verify_github_signature(self, request: web.Request, body: bytes) -> bool:
        """Verify GitHub webhook X-Hub-Signature-256. Returns True if valid or no secret configured."""
        secret = self.daemon.config.github_webhook_secret
        if not secret:
            return True  # No secret configured — skip verification
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not sig.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig[7:], expected)

    def _verify_stripe_signature(self, request: web.Request, body: bytes) -> bool:
        """Verify Stripe webhook Stripe-Signature header. Returns True if valid or no secret configured."""
        secret = self.daemon.config.stripe_webhook_secret
        if not secret:
            return True  # No secret configured — skip verification
        sig_header = request.headers.get("Stripe-Signature", "")
        if not sig_header:
            return False
        # Parse Stripe's "t=...,v1=..." format
        parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
        timestamp = parts.get("t", "")
        v1_sig = parts.get("v1", "")
        if not timestamp or not v1_sig:
            return False
        signed_payload = f"{timestamp}.{body.decode()}"
        expected = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(v1_sig, expected)

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
                "mcp_health": agent.check_mcp_health(),
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
        """POST /api/message — Send a message to an agent."""
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
        except Exception:
            log.exception("API message handler error")
            return web.json_response(
                {"error": "Internal server error"}, status=500,
            )

    async def _handle_message_stream(self, request: web.Request) -> web.StreamResponse:
        """POST /api/message/stream — Stream a response token by token (SSE)."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        message = body.get("message", "").strip()
        if not message:
            return web.json_response({"error": "Missing 'message' field"}, status=400)

        agent_name = body.get("agent")
        user_id = body.get("user_id", "api-user")

        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)

        try:
            async for chunk in self.daemon.handle_message_streaming(
                prompt=message,
                platform="api",
                user_id=user_id,
                agent_name=agent_name,
            ):
                if isinstance(chunk, str):
                    await resp.write(f"data: {json.dumps({'text': chunk})}\n\n".encode())
                    await resp.drain()
                else:
                    # ClaudeResponse final result — include error text if failed
                    from claude_daemon.core.process import ClaudeResponse
                    done_event: dict = {"done": True}
                    if isinstance(chunk, ClaudeResponse) and chunk.is_error:
                        done_event["error"] = chunk.result
                    await resp.write(f"data: {json.dumps(done_event)}\n\n".encode())
        except Exception:
            log.exception("API streaming handler error")
            await resp.write(f"data: {json.dumps({'error': 'Internal error'})}\n\n".encode())

        await resp.write_eof()
        return resp

    async def _handle_workflow(self, request: web.Request) -> web.Response:
        """POST /api/workflow — Run the build quality gate workflow."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        request_text = body.get("request", "").strip()
        if not request_text:
            return web.json_response({"error": "Missing 'request' field"}, status=400)

        max_cost = float(body.get("max_cost", 0.0))

        try:
            result = await self.daemon.run_build_workflow(
                request_text, max_total_cost=max_cost,
            )
            return web.json_response({"result": result})
        except Exception:
            log.exception("Workflow API error")
            return web.json_response({"error": "Workflow execution failed"}, status=500)

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """GET /api/metrics?agent=albert&days=7"""
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
        # Try index.html first, fall back to dashboard.html
        for name in ("index.html", "dashboard.html"):
            path = STATIC_DIR / name
            if path.exists():
                return web.FileResponse(path)
        return web.Response(text="Dashboard not found", status=404)

    async def _handle_discussions(self, request: web.Request) -> web.Response:
        """GET /api/discussions — list recent inter-agent discussions."""
        if not self.daemon.store:
            return web.json_response({"discussions": [], "stats": {}})
        dtype = request.query.get("type")
        limit = min(int(request.query.get("limit", "20")), 100)
        discussions = self.daemon.store.get_recent_discussions(discussion_type=dtype, limit=limit)
        stats = self.daemon.store.get_discussion_stats(days=7)
        return web.json_response({"discussions": discussions, "stats": stats})

    async def _handle_failures(self, request: web.Request) -> web.Response:
        """GET /api/failures — list recent failure analyses."""
        if not self.daemon.store:
            return web.json_response({"failures": [], "patterns": []})
        agent = request.query.get("agent")
        limit = min(int(request.query.get("limit", "20")), 100)
        failures = self.daemon.store.get_recent_failures(agent_name=agent, limit=limit)
        patterns = self.daemon.store.get_failure_patterns(days=7)
        return web.json_response({"failures": failures, "patterns": patterns})

    async def _handle_evolution(self, request: web.Request) -> web.Response:
        """GET /api/evolution — list evolution history."""
        if not self.daemon.store:
            return web.json_response({"evolution": []})
        agent = request.query.get("agent")
        limit = min(int(request.query.get("limit", "20")), 100)
        history = self.daemon.store.get_evolution_history(agent_name=agent, limit=limit)
        return web.json_response({"evolution": history})

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Receive external webhooks and route to appropriate agent.

        POST /api/webhook/{source}
        Verifies signatures for GitHub and Stripe. Processes asynchronously
        and returns 202 Accepted immediately.
        """
        source = request.match_info["source"]

        try:
            raw_body = await request.read()
            body = json.loads(raw_body) if raw_body else {}
        except (json.JSONDecodeError, Exception):
            body = {}
            raw_body = b""

        # Verify webhook signatures
        if source == "github" and not self._verify_github_signature(request, raw_body):
            log.warning("GitHub webhook signature verification failed")
            return web.json_response({"error": "Invalid signature"}, status=403)

        if source == "stripe" and not self._verify_stripe_signature(request, raw_body):
            log.warning("Stripe webhook signature verification failed")
            return web.json_response({"error": "Invalid signature"}, status=403)

        # Build prompt and determine agent
        agent_name = None
        prompt = ""

        if source == "github":
            event_type = request.headers.get("X-GitHub-Event", "unknown")
            prompt = (
                f"[GitHub webhook: {event_type}]\n\n"
                f"{json.dumps(body, indent=2)[:3000]}"
            )
            if event_type in ("pull_request", "pull_request_review"):
                agent_name = "max"
            elif event_type == "push":
                agent_name = "albert"
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
            # Generic webhook — must have API key auth
            if self.api_key:
                auth = request.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or auth[7:] != self.api_key:
                    return web.json_response({"error": "Unauthorized"}, status=401)
            prompt = (
                f"[Webhook from {source}]\n\n"
                f"{json.dumps(body, indent=2)[:3000]}"
            )
            agent_name = "johnny"

        if not prompt:
            return web.json_response({"status": "ignored"})

        # Audit incoming webhook
        if self.daemon.store:
            self.daemon.store.record_audit(
                action="webhook_receive", platform=f"webhook:{source}",
                details=f"agent={agent_name}",
            )

        # Process asynchronously — return 202 immediately
        async def _process():
            try:
                await self.daemon.handle_message(
                    prompt=prompt,
                    platform=f"webhook:{source}",
                    user_id=f"webhook:{source}",
                    agent_name=agent_name,
                )
            except Exception:
                log.exception("Webhook async processing failed for %s", source)

        asyncio.create_task(_process())
        return web.json_response(
            {"status": "accepted", "agent": agent_name}, status=202,
        )

    async def _handle_paperclip_heartbeat(self, request: web.Request) -> web.Response:
        """POST /api/paperclip/heartbeat — Receive a Paperclip heartbeat.

        Paperclip pushes tasks to agents via webhooks. This endpoint accepts
        the heartbeat payload, routes the task to the appropriate daemon agent,
        and returns the result synchronously.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        # Find the Paperclip integration
        pc = self.daemon.router.integrations.get("paperclip") if self.daemon.router else None
        if not pc:
            # No Paperclip integration running — handle directly via daemon
            prompt = body.get("prompt") or body.get("description", "")
            if not prompt:
                task = body.get("task", {})
                prompt = task.get("prompt") or task.get("description", "")
            if not prompt:
                return web.json_response({"error": "No prompt in payload"}, status=400)

            agent_name = body.get("agent") or body.get("assigned_to")
            try:
                result = await self.daemon.handle_message(
                    prompt=prompt,
                    platform="paperclip",
                    user_id=body.get("created_by", "paperclip"),
                    agent_name=agent_name,
                )
                return web.json_response({"status": "ok", "result": result})
            except Exception:
                log.exception("Paperclip heartbeat handler error")
                return web.json_response({"error": "Processing failed"}, status=500)

        # Route through Paperclip integration (has handler + cost reporting)
        try:
            result = await pc.handle_heartbeat(body)
            status = 200 if result.get("status") == "ok" else 500
            return web.json_response(result, status=status)
        except Exception:
            log.exception("Paperclip heartbeat handler error")
            return web.json_response({"error": "Processing failed"}, status=500)

    async def _handle_audit(self, request: web.Request) -> web.Response:
        """GET /api/audit?action=agent_message&agent=albert&limit=50&offset=0"""
        if not self.daemon.store:
            return web.json_response({"audit": []})
        action = request.query.get("action")
        agent = request.query.get("agent")
        limit = int(request.query.get("limit", "100"))
        offset = int(request.query.get("offset", "0"))
        entries = self.daemon.store.get_audit_log(
            action=action, agent_name=agent, limit=limit, offset=offset,
        )
        return web.json_response({"audit": entries})

    async def _handle_env_list(self, request: web.Request) -> web.Response:
        """GET /api/config/env — list env vars with set/unset status (masked)."""
        from claude_daemon.core.env_manager import list_env_vars
        return web.json_response({"env_vars": list_env_vars()})

    async def _handle_env_set(self, request: web.Request) -> web.Response:
        """POST /api/config/env — set an env var. Body: {"key": "...", "value": "..."}"""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON"}, status=400)

        key = body.get("key", "").strip().upper()
        value = body.get("value", "").strip()
        if not key or not value:
            return web.json_response({"error": "Missing 'key' or 'value'"}, status=400)

        try:
            from claude_daemon.core.env_manager import set_env_var, reload_env
            set_env_var(key, value)
            reload_env()
            await self.daemon.reload_config()

            masked = "****" + value[-4:] if len(value) >= 4 else "****"
            if self.daemon.store:
                self.daemon.store.record_audit(
                    action="env_set", details=f"key={key}",
                )
            return web.json_response({"status": "ok", "key": key, "masked": masked})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    # -- MCP server pool --

    async def _handle_mcp_list(self, request: web.Request) -> web.Response:
        """GET /api/mcp — list all MCP servers with tier and status."""
        statuses = self.daemon.get_mcp_status()
        return web.json_response({"servers": statuses})

    async def _handle_mcp_enable(self, request: web.Request) -> web.Response:
        """POST /api/mcp/enable — enable a disabled server."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON"}, status=400)
        server = body.get("server", "").strip()
        if not server:
            return web.json_response({"error": "Missing 'server'"}, status=400)
        result = await self.daemon.enable_mcp_server(server)
        return web.json_response({"status": "ok", "message": result})

    async def _handle_mcp_disable(self, request: web.Request) -> web.Response:
        """POST /api/mcp/disable — disable a server."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON"}, status=400)
        server = body.get("server", "").strip()
        if not server:
            return web.json_response({"error": "Missing 'server'"}, status=400)
        result = await self.daemon.disable_mcp_server(server)
        return web.json_response({"status": "ok", "message": result})

    async def _handle_mcp_refresh(self, request: web.Request) -> web.Response:
        """POST /api/mcp/refresh — regenerate tools.json for all agents."""
        result = await self.daemon.refresh_mcp()
        return web.json_response({"status": "ok", "message": result})

    # -- Settings --

    async def _handle_settings_thinking(self, request: web.Request) -> web.Response:
        """POST /api/settings/thinking — toggle extended thinking."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON"}, status=400)
        enabled = body.get("enabled")
        if enabled is None or not isinstance(enabled, bool):
            return web.json_response({"error": "Missing 'enabled' (bool)"}, status=400)
        result = await self.daemon.set_thinking(enabled)
        return web.json_response({"status": "ok", "message": result})

    async def _handle_settings_effort(self, request: web.Request) -> web.Response:
        """POST /api/settings/effort — set reasoning effort level."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON"}, status=400)
        level = body.get("level", "").strip().lower()
        if not level:
            return web.json_response({"error": "Missing 'level'"}, status=400)
        result = await self.daemon.set_default_effort(level)
        return web.json_response({"status": "ok", "message": result})

    async def _handle_backend_status(self, request: web.Request) -> web.Response:
        """GET /api/settings/backend — get Managed Agents backend status."""
        status = self.daemon.get_managed_agents_status()
        return web.json_response(status)

    async def _handle_backend_set(self, request: web.Request) -> web.Response:
        """POST /api/settings/backend — enable/disable Managed Agents backend."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON"}, status=400)
        enabled = body.get("enabled")
        if enabled is None:
            return web.json_response({"error": "Missing 'enabled' (true/false)"}, status=400)
        result = await self.daemon.set_managed_agents(bool(enabled))
        return web.json_response({"status": "ok", "message": result})
