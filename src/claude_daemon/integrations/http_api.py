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
from claude_daemon.orchestration import TaskAPI, TaskSubmission
from claude_daemon.orchestration.budgets import BudgetStore
from claude_daemon.orchestration.approvals import ApprovalsStore
from claude_daemon.orchestration.goals import GoalsStore

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
        self._task_api: TaskAPI | None = None
        self._budget_store: BudgetStore | None = None
        self._goals_store: GoalsStore | None = None
        self._approvals_store: ApprovalsStore | None = None
        self._setup_routes()

    def _get_budget_store(self) -> BudgetStore | None:
        """Lazily construct BudgetStore once store is ready."""
        if self._budget_store is not None:
            return self._budget_store
        if not self.daemon.store:
            return None
        self._budget_store = BudgetStore(self.daemon.store)
        if self.daemon.orchestrator:
            self.daemon.orchestrator.set_budget_store(self._budget_store)
        return self._budget_store

    def _get_approvals_store(self) -> ApprovalsStore | None:
        """Lazily construct ApprovalsStore once store is ready."""
        if self._approvals_store is not None:
            return self._approvals_store
        if not self.daemon.store:
            return None
        self._approvals_store = ApprovalsStore(self.daemon.store)
        return self._approvals_store

    def _get_goals_store(self) -> GoalsStore | None:
        """Lazily construct GoalsStore once store is ready."""
        if self._goals_store is not None:
            return self._goals_store
        if not self.daemon.store:
            return None
        self._goals_store = GoalsStore(self.daemon.store)
        return self._goals_store

    def _get_task_api(self) -> TaskAPI | None:
        """Lazily construct TaskAPI once orchestrator is ready."""
        if self._task_api is not None:
            return self._task_api
        if not (self.daemon.orchestrator and self.daemon.store and self.daemon.agent_registry):
            return None
        self._task_api = TaskAPI(
            orchestrator=self.daemon.orchestrator,
            registry=self.daemon.agent_registry,
            store=self.daemon.store,
            budget_store=self._get_budget_store(),
            approvals_store=self._get_approvals_store(),
        )
        return self._task_api

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
        # Native orchestration API (absorbs Paperclip task-queue capabilities)
        self._app.router.add_post("/api/v1/tasks", self._handle_v1_tasks_submit)
        self._app.router.add_get("/api/v1/tasks/pending", self._handle_v1_tasks_pending)
        self._app.router.add_get("/api/v1/tasks/recent", self._handle_v1_tasks_recent)
        self._app.router.add_get("/api/v1/tasks/{task_id}", self._handle_v1_tasks_get)
        self._app.router.add_post("/api/v1/tasks/{task_id}/cancel", self._handle_v1_tasks_cancel)
        # Budget management (Phase 2)
        self._app.router.add_get("/api/v1/budgets", self._handle_v1_budgets_list)
        self._app.router.add_post("/api/v1/budgets", self._handle_v1_budgets_create)
        self._app.router.add_get("/api/v1/budgets/{budget_id}", self._handle_v1_budgets_get)
        self._app.router.add_put("/api/v1/budgets/{budget_id}", self._handle_v1_budgets_update)
        self._app.router.add_delete("/api/v1/budgets/{budget_id}", self._handle_v1_budgets_delete)
        # Goal tracking (Phase 3)
        self._app.router.add_get("/api/v1/goals", self._handle_v1_goals_list)
        self._app.router.add_post("/api/v1/goals", self._handle_v1_goals_create)
        self._app.router.add_get("/api/v1/goals/{goal_id}", self._handle_v1_goals_get)
        self._app.router.add_put("/api/v1/goals/{goal_id}", self._handle_v1_goals_update)
        self._app.router.add_delete("/api/v1/goals/{goal_id}", self._handle_v1_goals_delete)
        self._app.router.add_get("/api/v1/goals/{goal_id}/progress", self._handle_v1_goals_progress)
        # Approvals (Phase 4)
        self._app.router.add_get("/api/v1/approvals", self._handle_v1_approvals_list)
        self._app.router.add_post("/api/v1/approvals/{approval_id}/approve", self._handle_v1_approvals_approve)
        self._app.router.add_post("/api/v1/approvals/{approval_id}/reject", self._handle_v1_approvals_reject)
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
            self._app.router.add_post(
                "/dashboard/login", self._handle_dashboard_login,
            )
            log.info("Dashboard enabled — serving at http://localhost:%d/", self.port)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # Public endpoints: health check, dashboard shell, login form, static assets.
        # (The dashboard handler does its own auth dance — cookie, ?key=, or login page.)
        if (
            request.path in ("/api/health", "/", "/dashboard/login")
            or request.path.startswith("/static/")
        ):
            return await handler(request)

        # Webhooks use their own signature verification, not bearer tokens
        if request.path.startswith("/api/webhook/"):
            return await handler(request)

        if self.api_key:
            auth = request.headers.get("Authorization", "")
            # WebSocket: accept token via Sec-WebSocket-Protocol header
            ws_proto = request.headers.get("Sec-WebSocket-Protocol", "")
            # Browser dashboard: accept cookie set by /dashboard/login
            cookie_token = request.cookies.get("cd_session", "")
            token = ""
            if auth.startswith("Bearer "):
                token = auth[7:]
            elif ws_proto:
                token = ws_proto
            elif cookie_token:
                token = cookie_token
            if not token or not hmac.compare_digest(token, self.api_key):
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

        agent_costs: dict[str, float] = {}
        if self.daemon.store:
            try:
                rows = self.daemon.store._db.execute(
                    "SELECT user_id, COALESCE(SUM(total_cost_usd), 0) as cost "
                    "FROM conversations GROUP BY user_id"
                ).fetchall()
                for r in rows:
                    parts = str(r["user_id"]).rsplit(":", 1)
                    if len(parts) == 2:
                        agent_costs[parts[1]] = agent_costs.get(parts[1], 0) + r["cost"]
            except Exception:
                pass

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
                "cost": agent_costs.get(agent.name, 0),
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
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
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
        """GET /api/tasks — merged view of live spawned + recent persisted tasks."""
        if not self.daemon.orchestrator:
            return web.json_response({"tasks": []})

        # Collect live tasks from orchestrator
        live: dict[str, dict] = {}
        for t in self.daemon.orchestrator.list_tasks():
            live[t.task_id] = {
                "task_id": t.task_id,
                "agent": t.agent_name,
                "status": t.status,
                "prompt": t.prompt[:200],
                "cost": t.cost,
            }

        # Merge in recent persisted rows (DB truth)
        tasks = list(live.values())
        if self.daemon.store:
            seen = set(live.keys())
            for row in self.daemon.store.get_recent_tasks(limit=100):
                tid = row.get("id")
                if tid in seen:
                    continue
                tasks.append({
                    "task_id": tid,
                    "agent": row.get("agent_name"),
                    "status": row.get("status"),
                    "prompt": (row.get("prompt") or "")[:200],
                    "cost": row.get("cost_usd") or 0.0,
                })
        return web.json_response({"tasks": tasks})

    # -- /api/v1/tasks — native orchestration API --

    async def _handle_v1_tasks_submit(self, request: web.Request) -> web.Response:
        api = self._get_task_api()
        if api is None:
            return web.json_response({"error": "Orchestrator not ready"}, status=503)
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        prompt = body.get("prompt") or body.get("description", "")
        if not prompt:
            return web.json_response({"error": "Missing 'prompt'"}, status=400)

        req = TaskSubmission(
            prompt=prompt,
            agent=body.get("agent") or body.get("assigned_to"),
            user_id=body.get("user_id") or body.get("user") or body.get("created_by", "api"),
            task_type=body.get("task_type", "default"),
            platform=body.get("platform", "api"),
            goal_id=body.get("goal_id"),
            metadata=body.get("metadata") or {},
        )
        result = api.submit_task(req)
        status_map = {"pending": 201, "pending_approval": 202, "rejected": 400, "error": 500}
        http_status = status_map.get(result.status, 400)
        return web.json_response(result.to_dict(), status=http_status)

    async def _handle_v1_tasks_pending(self, request: web.Request) -> web.Response:
        api = self._get_task_api()
        if api is None:
            return web.json_response({"tasks": []})
        agent = request.query.get("agent")
        limit = min(int(request.query.get("limit", "50")), 200)
        return web.json_response({"tasks": api.list_pending(agent=agent, limit=limit)})

    async def _handle_v1_tasks_recent(self, request: web.Request) -> web.Response:
        api = self._get_task_api()
        if api is None:
            return web.json_response({"tasks": []})
        limit = min(int(request.query.get("limit", "50")), 200)
        return web.json_response({"tasks": api.list_recent(limit=limit)})

    async def _handle_v1_tasks_get(self, request: web.Request) -> web.Response:
        api = self._get_task_api()
        if api is None:
            return web.json_response({"error": "Orchestrator not ready"}, status=503)
        task_id = request.match_info["task_id"]
        row = api.get_task(task_id)
        if row is None:
            return web.json_response({"error": "Task not found"}, status=404)
        return web.json_response(row)

    async def _handle_v1_tasks_cancel(self, request: web.Request) -> web.Response:
        api = self._get_task_api()
        if api is None:
            return web.json_response({"error": "Orchestrator not ready"}, status=503)
        task_id = request.match_info["task_id"]
        result = await api.cancel_task(task_id)
        return web.json_response(result)

    # -- /api/v1/budgets — budget management --

    async def _handle_v1_budgets_list(self, request: web.Request) -> web.Response:
        bs = self._get_budget_store()
        if bs is None:
            return web.json_response({"budgets": []})
        enabled_only = request.query.get("enabled") == "1"
        return web.json_response({"budgets": bs.list_all(enabled_only=enabled_only)})

    async def _handle_v1_budgets_create(self, request: web.Request) -> web.Response:
        bs = self._get_budget_store()
        if bs is None:
            return web.json_response({"error": "Store not ready"}, status=503)
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        try:
            bid = bs.create(
                scope=body.get("scope", ""),
                limit_usd=float(body.get("limit_usd", 0)),
                period=body.get("period", ""),
                scope_value=body.get("scope_value"),
                approval_threshold_usd=body.get("approval_threshold_usd"),
            )
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"id": bid, "status": "created"}, status=201)

    async def _handle_v1_budgets_get(self, request: web.Request) -> web.Response:
        bs = self._get_budget_store()
        if bs is None:
            return web.json_response({"error": "Store not ready"}, status=503)
        try:
            bid = int(request.match_info["budget_id"])
        except ValueError:
            return web.json_response({"error": "Invalid budget_id"}, status=400)
        row = bs.get(bid)
        if row is None:
            return web.json_response({"error": "Budget not found"}, status=404)
        return web.json_response(row)

    async def _handle_v1_budgets_update(self, request: web.Request) -> web.Response:
        bs = self._get_budget_store()
        if bs is None:
            return web.json_response({"error": "Store not ready"}, status=503)
        try:
            bid = int(request.match_info["budget_id"])
        except ValueError:
            return web.json_response({"error": "Invalid budget_id"}, status=400)
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        kwargs: dict = {}
        if "limit_usd" in body:
            kwargs["limit_usd"] = float(body["limit_usd"])
        if "period" in body:
            kwargs["period"] = body["period"]
        if "enabled" in body:
            kwargs["enabled"] = bool(body["enabled"])
        if "approval_threshold_usd" in body:
            kwargs["approval_threshold_usd"] = body["approval_threshold_usd"]
        try:
            ok = bs.update(bid, **kwargs)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        if not ok:
            return web.json_response({"error": "Budget not found"}, status=404)
        return web.json_response({"status": "updated"})

    async def _handle_v1_budgets_delete(self, request: web.Request) -> web.Response:
        bs = self._get_budget_store()
        if bs is None:
            return web.json_response({"error": "Store not ready"}, status=503)
        try:
            bid = int(request.match_info["budget_id"])
        except ValueError:
            return web.json_response({"error": "Invalid budget_id"}, status=400)
        ok = bs.delete(bid)
        if not ok:
            return web.json_response({"error": "Budget not found"}, status=404)
        return web.json_response({"status": "deleted"})

    # -- /api/v1/goals — goal tracking --

    async def _handle_v1_goals_list(self, request: web.Request) -> web.Response:
        gs = self._get_goals_store()
        if gs is None:
            return web.json_response({"goals": []})
        status = request.query.get("status")
        owner = request.query.get("owner_agent")
        return web.json_response({"goals": gs.list_all(status=status, owner_agent=owner)})

    async def _handle_v1_goals_create(self, request: web.Request) -> web.Response:
        gs = self._get_goals_store()
        if gs is None:
            return web.json_response({"error": "Store not ready"}, status=503)
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        try:
            gid = gs.create(
                title=body.get("title", ""),
                description=body.get("description"),
                owner_agent=body.get("owner_agent"),
                target_date=body.get("target_date"),
                parent_goal_id=body.get("parent_goal_id"),
            )
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        try:
            await self.hub.goal_update(gid, body.get("title", ""), "active")
        except Exception:
            pass
        return web.json_response({"id": gid, "status": "created"}, status=201)

    async def _handle_v1_goals_get(self, request: web.Request) -> web.Response:
        gs = self._get_goals_store()
        if gs is None:
            return web.json_response({"error": "Store not ready"}, status=503)
        try:
            gid = int(request.match_info["goal_id"])
        except ValueError:
            return web.json_response({"error": "Invalid goal_id"}, status=400)
        row = gs.get(gid)
        if row is None:
            return web.json_response({"error": "Goal not found"}, status=404)
        return web.json_response(row)

    async def _handle_v1_goals_update(self, request: web.Request) -> web.Response:
        gs = self._get_goals_store()
        if gs is None:
            return web.json_response({"error": "Store not ready"}, status=503)
        try:
            gid = int(request.match_info["goal_id"])
        except ValueError:
            return web.json_response({"error": "Invalid goal_id"}, status=400)
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        kwargs: dict = {}
        if "title" in body:
            kwargs["title"] = body["title"]
        if "description" in body:
            kwargs["description"] = body["description"]
        if "owner_agent" in body:
            kwargs["owner_agent"] = body["owner_agent"]
        if "target_date" in body:
            kwargs["target_date"] = body["target_date"]
        if "status" in body:
            kwargs["status"] = body["status"]
        try:
            ok = gs.update(gid, **kwargs)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        if not ok:
            return web.json_response({"error": "Goal not found"}, status=404)
        try:
            row = gs.get(gid)
            await self.hub.goal_update(gid, row["title"], row["status"])
        except Exception:
            pass
        return web.json_response({"status": "updated"})

    async def _handle_v1_goals_delete(self, request: web.Request) -> web.Response:
        gs = self._get_goals_store()
        if gs is None:
            return web.json_response({"error": "Store not ready"}, status=503)
        try:
            gid = int(request.match_info["goal_id"])
        except ValueError:
            return web.json_response({"error": "Invalid goal_id"}, status=400)
        ok = gs.delete(gid)
        if not ok:
            return web.json_response({"error": "Goal not found"}, status=404)
        return web.json_response({"status": "deleted"})

    async def _handle_v1_goals_progress(self, request: web.Request) -> web.Response:
        gs = self._get_goals_store()
        if gs is None:
            return web.json_response({"error": "Store not ready"}, status=503)
        try:
            gid = int(request.match_info["goal_id"])
        except ValueError:
            return web.json_response({"error": "Invalid goal_id"}, status=400)
        row = gs.get(gid)
        if row is None:
            return web.json_response({"error": "Goal not found"}, status=404)
        progress = gs.compute_progress(gid)
        return web.json_response({"goal_id": gid, **progress})

    # -- /api/v1/approvals — approval queue --

    async def _handle_v1_approvals_list(self, request: web.Request) -> web.Response:
        appr = self._get_approvals_store()
        if appr is None:
            return web.json_response({"approvals": []})
        pending_only = request.query.get("pending") == "1"
        if pending_only:
            return web.json_response({"approvals": appr.list_pending()})
        limit = min(int(request.query.get("limit", "50")), 200)
        return web.json_response({"approvals": appr.list_all(limit=limit)})

    async def _handle_v1_approvals_approve(self, request: web.Request) -> web.Response:
        """Approve a pending approval and dispatch the linked task.

        Flow: load approval → load task → re-enforce budget (skip threshold
        since user already approved) → stash fresh reservations on task →
        atomically transition approval + task → spawn → broadcast.  Any
        failure rolls back the preceding step so the task can never end up
        in an inconsistent state.
        """
        appr = self._get_approvals_store()
        if appr is None:
            return web.json_response({"error": "Store not ready"}, status=503)
        try:
            aid = int(request.match_info["approval_id"])
        except ValueError:
            return web.json_response({"error": "Invalid approval_id"}, status=400)
        approver = "api"
        try:
            body = await request.json()
            approver = body.get("approver", "api")
        except Exception:
            pass

        # Load approval + linked task without mutating state.
        approval_row = appr.get(aid)
        if approval_row is None:
            return web.json_response({"error": "Approval not found"}, status=404)
        if approval_row["status"] != "pending":
            return web.json_response(
                {"error": f"Approval already {approval_row['status']}"},
                status=409,
            )
        task_id = approval_row["task_id"]
        task = self.daemon.store.get_task(task_id) if self.daemon.store else None
        if not task:
            return web.json_response({"error": "Linked task missing"}, status=500)

        # Re-enforce budget — the situation may have changed during the wait.
        budget_store = self._get_budget_store()
        reservations: list[tuple[int, float]] = []
        if budget_store is not None:
            from claude_daemon.orchestration.enforcement import enforce_budget
            decision = enforce_budget(
                budget_store,
                agent_name=task.get("agent_name"),
                user_id=task.get("user_id"),
                task_type=task.get("task_type"),
                skip_approval_threshold=True,
            )
            if decision.outcome == "rejected":
                return web.json_response(
                    {"error": decision.reason,
                     "detail": "Budget exhausted during approval wait"},
                    status=409,
                )
            reservations = decision.reservations

        # Persist fresh reservations before dispatch so completion can drain them.
        if reservations and self.daemon.store:
            self.daemon.store.update_task_metadata(
                task_id,
                {"_budget_reservations": [[bid, amt] for bid, amt in reservations]},
            )

        # Atomic transition: approvals.pending → approved AND
        # task_queue.pending_approval → pending.
        if not appr.approve(aid, approver=approver):
            if reservations and budget_store:
                budget_store.release_reservations(reservations)
            return web.json_response(
                {"error": "Approval not found or already resolved"},
                status=409,
            )

        # Dispatch.
        agent_name = task.get("agent_name")
        agent = (
            self.daemon.agent_registry.get(agent_name)
            if (agent_name and self.daemon.agent_registry) else None
        )
        if self.daemon.orchestrator and agent:
            try:
                self.daemon.orchestrator.spawn_task(
                    agent=agent,
                    prompt=task.get("prompt", ""),
                    platform=task.get("platform", "api"),
                    user_id=task.get("user_id", "local"),
                    task_type=task.get("task_type", "default"),
                    task_id=task_id,
                )
            except Exception:
                log.exception("Failed to dispatch approved task %s", task_id)
                # Rollback: release fresh reservations and mark task failed.
                if reservations and budget_store:
                    budget_store.release_reservations(reservations)
                try:
                    self.daemon.store.update_task_status(
                        task_id, "failed",
                        error="dispatch failed after approve",
                    )
                except Exception:
                    log.exception("Could not mark task %s failed", task_id)
                return web.json_response(
                    {"error": "Dispatch failed after approve"}, status=500,
                )

        try:
            await self.hub.approval_resolved(aid, task_id, "approved", approver)
        except Exception:
            pass
        return web.json_response({"status": "approved"})

    async def _handle_v1_approvals_reject(self, request: web.Request) -> web.Response:
        appr = self._get_approvals_store()
        if appr is None:
            return web.json_response({"error": "Store not ready"}, status=503)
        try:
            aid = int(request.match_info["approval_id"])
        except ValueError:
            return web.json_response({"error": "Invalid approval_id"}, status=400)
        approver = "api"
        try:
            body = await request.json()
            approver = body.get("approver", "api")
        except Exception:
            pass
        ok = appr.reject(aid, approver=approver)
        if not ok:
            return web.json_response(
                {"error": "Approval not found or already resolved"}, status=409,
            )
        row = appr.get(aid)
        try:
            if row:
                await self.hub.approval_resolved(aid, row["task_id"], "rejected", approver)
        except Exception:
            pass
        return web.json_response({"status": "rejected"})

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint for live dashboard events."""
        return await self.hub.ws_handler(request)

    async def _handle_dashboard(self, request: web.Request) -> web.Response:
        """Serve the dashboard — with cookie-backed auth when api_key is set.

        Flow:
        - No api_key configured → serve the dashboard directly (legacy behaviour).
        - Request has ?key=<k> → validate; on match, set session cookie and 302
          to `/` (stripping the query); on mismatch, 403.
        - Request has a valid cd_session cookie → serve the dashboard.
        - Otherwise → serve the login page.
        """
        if not self.api_key:
            return self._serve_dashboard_html()

        # Trusted local access: auto-set session cookie for localhost
        host = request.host.split(":")[0]
        if host in ("localhost", "127.0.0.1", "::1"):
            cookie = request.cookies.get("cd_session", "")
            if cookie and hmac.compare_digest(cookie, self.api_key):
                return self._serve_dashboard_html()
            return self._make_session_redirect()

        key_param = request.query.get("key")
        if key_param is not None:
            if hmac.compare_digest(key_param, self.api_key):
                return self._make_session_redirect()
            return web.Response(
                text="Invalid API key", status=403, content_type="text/html",
            )

        cookie = request.cookies.get("cd_session", "")
        if cookie and hmac.compare_digest(cookie, self.api_key):
            return self._serve_dashboard_html()

        return self._serve_login_html()

    async def _handle_dashboard_login(self, request: web.Request) -> web.Response:
        """Handle POST from the login form — sets cookie, redirects to /."""
        if not self.api_key:
            return web.Response(status=302, headers={"Location": "/"})
        data = await request.post()
        key = str(data.get("key", ""))
        if key and hmac.compare_digest(key, self.api_key):
            return self._make_session_redirect()
        return web.Response(
            text="Invalid API key", status=403, content_type="text/html",
        )

    def _serve_dashboard_html(self) -> web.Response:
        for name in ("index.html", "dashboard.html"):
            path = STATIC_DIR / name
            if path.exists():
                return web.FileResponse(path)
        return web.Response(text="Dashboard not found", status=404)

    def _serve_login_html(self) -> web.Response:
        path = STATIC_DIR / "login.html"
        if path.exists():
            return web.FileResponse(path, status=401)
        return web.Response(text="Login page missing", status=500)

    def _make_session_redirect(self) -> web.Response:
        resp = web.Response(status=302, headers={"Location": "/"})
        resp.set_cookie(
            "cd_session", self.api_key,
            max_age=30 * 24 * 3600,
            httponly=True, samesite="Strict", path="/",
        )
        return resp

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
        """POST /api/paperclip/heartbeat — Paperclip compat shim.

        Accepts the Paperclip heartbeat payload shape and forwards it to the
        native task API. Preserves cost-reporting to Paperclip when the
        outbound integration is active (via its message handler).
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        # Prefer the outbound Paperclip integration (provides cost reporting)
        pc = self.daemon.router.integrations.get("paperclip") if self.daemon.router else None
        if pc:
            try:
                result = await pc.handle_heartbeat(body)
                status = 200 if result.get("status") == "ok" else 500
                return web.json_response(result, status=status)
            except Exception:
                log.exception("Paperclip heartbeat handler error")
                return web.json_response({"error": "Processing failed"}, status=500)

        # No outbound integration — forward to native task API
        api = self._get_task_api()
        if api is None:
            return web.json_response({"error": "Orchestrator not ready"}, status=503)

        task = body.get("task") or body
        prompt = task.get("prompt") or task.get("description", "")
        if not prompt:
            return web.json_response({"error": "No prompt in payload"}, status=400)

        external_id = task.get("id") or task.get("task_id")
        req = TaskSubmission(
            prompt=prompt,
            agent=task.get("agent") or task.get("assigned_to"),
            user_id=task.get("created_by", "paperclip"),
            task_type=task.get("task_type", "default"),
            platform="paperclip",
            metadata={"paperclip_task_id": external_id, "heartbeat": True},
        )
        result = api.submit_task(req)
        if result.status == "pending":
            return web.json_response(
                {"status": "ok", "task_id": result.task_id}, status=200,
            )
        return web.json_response(
            {"status": "error", "error": result.error or "submission failed"},
            status=400,
        )

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
