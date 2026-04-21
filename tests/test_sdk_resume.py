"""Tests for lazy SDK session resume across daemon restart.

Covers the three wiring layers:
  * `SDKBridgeManager._resumed` tracking + graceful fallback on a failed resume
  * `ProcessManager.ensure_agent_session(resume_session_id=...)` plumbing
  * `Orchestrator._pick_resume_session` freshness check
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_daemon.core.process import ClaudeResponse
from claude_daemon.core.sdk_bridge import SDKBridgeManager


# ── SDKBridgeManager ────────────────────────────────────────────────────────


def _bridge_config() -> SimpleNamespace:
    return SimpleNamespace(
        sdk_bridge_idle_timeout_ms=90_000,
        sdk_create_session_timeout_ms=5_000,
        process_timeout=300,
        sdk_bridge_node_path="node",
        permission_mode="auto",
    )


def _alive_bridge() -> SDKBridgeManager:
    """Build a bridge whose `is_alive` is True and stdin is mocked out."""
    mgr = SDKBridgeManager(_bridge_config())
    # Fake the subprocess so `is_alive` returns True without a real process.
    fake = MagicMock()
    fake.returncode = None
    mgr._process = fake
    mgr._send_command = AsyncMock()
    return mgr


@pytest.mark.asyncio
async def test_create_session_marks_resumed_on_success():
    """A successful resume marks the key so subsequent calls are no-ops."""
    mgr = _alive_bridge()

    # Simulate the bridge answering the create command with a created event.
    async def fake_send(cmd):
        req_id = cmd["id"]
        future = mgr._pending.get(req_id)
        if future and not future.done():
            future.set_result({
                "event": "created",
                "id": req_id,
                "agent": cmd["agent"],
                "sessionId": "sess-123",
            })
    mgr._send_command.side_effect = fake_send

    sid = await mgr.create_session(
        agent_name="johnny", model="sonnet",
        resume_session_id="sess-123",
    )
    assert sid == "sess-123"
    assert mgr.has_session("johnny", "sonnet")
    assert mgr.has_resumed("johnny", "sonnet")


@pytest.mark.asyncio
async def test_create_session_falls_back_when_resume_errors():
    """A failed resume retries once without resume_session_id, and still marks _resumed."""
    mgr = _alive_bridge()

    call_count = {"n": 0}

    async def fake_send(cmd):
        call_count["n"] += 1
        req_id = cmd["id"]
        future = mgr._pending.get(req_id)
        if future and not future.done():
            if call_count["n"] == 1:
                assert cmd["resumeSessionId"] == "stale-sess"
                future.set_result({
                    "event": "error",
                    "id": req_id,
                    "message": "resume failed: session expired",
                    "recoverable": False,
                })
            else:
                assert cmd["resumeSessionId"] is None
                future.set_result({
                    "event": "created",
                    "id": req_id,
                    "agent": cmd["agent"],
                    "sessionId": "fresh-sess",
                })
    mgr._send_command.side_effect = fake_send

    sid = await mgr.create_session(
        agent_name="johnny", model="sonnet",
        resume_session_id="stale-sess",
    )
    assert sid == "fresh-sess"
    assert call_count["n"] == 2
    assert mgr.has_session("johnny", "sonnet")
    assert mgr.has_resumed("johnny", "sonnet")


@pytest.mark.asyncio
async def test_create_session_no_resume_does_not_mark():
    """A normal (non-resume) create should not mark _resumed."""
    mgr = _alive_bridge()

    async def fake_send(cmd):
        req_id = cmd["id"]
        future = mgr._pending.get(req_id)
        if future and not future.done():
            future.set_result({
                "event": "created",
                "id": req_id,
                "agent": cmd["agent"],
                "sessionId": "new-sess",
            })
    mgr._send_command.side_effect = fake_send

    await mgr.create_session(agent_name="johnny", model="sonnet")
    assert mgr.has_session("johnny", "sonnet")
    assert not mgr.has_resumed("johnny", "sonnet")


@pytest.mark.asyncio
async def test_shutdown_clears_resumed_set():
    """Shutdown wipes _resumed so next daemon-life starts clean."""
    mgr = _alive_bridge()
    mgr._resumed.add("johnny:sonnet")
    mgr._sessions["johnny:sonnet"] = "sess-1"

    # Make shutdown's _send_command and process.kill no-ops.
    async def answer_shutdown(cmd):
        req_id = cmd["id"]
        future = mgr._pending.get(req_id)
        if future and not future.done():
            future.set_result({"event": "shutdown", "id": req_id})
    mgr._send_command.side_effect = answer_shutdown
    mgr._process.kill = MagicMock()

    async def fake_wait():
        return None
    mgr._process.wait = fake_wait

    await mgr.shutdown()
    assert mgr._resumed == set()
    assert mgr._sessions == {}


# ── ProcessManager.ensure_agent_session ─────────────────────────────────────


class _FakeBridge:
    """Minimal bridge stand-in for ensure_agent_session tests."""

    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.resumed: set[str] = set()
        self.created: list[dict] = []
        self.closed: list[str] = []

    @staticmethod
    def _key(agent: str, model: str | None) -> str:
        return f"{agent}:{model or 'default'}"

    def has_session(self, agent: str, model: str | None = None) -> bool:
        return self._key(agent, model) in self.sessions

    def has_resumed(self, agent: str, model: str | None = None) -> bool:
        return self._key(agent, model) in self.resumed

    async def close_session(self, agent: str, model: str | None = None) -> None:
        key = self._key(agent, model)
        self.closed.append(key)
        self.sessions.discard(key)

    async def create_session(self, **kwargs) -> str:
        key = self._key(kwargs["agent_name"], kwargs.get("model"))
        self.created.append(kwargs)
        self.sessions.add(key)
        if kwargs.get("resume_session_id"):
            self.resumed.add(key)
        return "new-sess"


def _process_manager_with(bridge: _FakeBridge):
    """Minimal ProcessManager with ensure_sdk_bridge returning `bridge`."""
    from claude_daemon.core.process import ProcessManager
    pm = ProcessManager.__new__(ProcessManager)
    pm.config = SimpleNamespace(sdk_resume_max_age_hours=24)
    pm._sdk_bridge = bridge
    pm._sdk_bridge_disabled = False

    async def ensure():
        return bridge
    pm.ensure_sdk_bridge = ensure
    return pm


@pytest.mark.asyncio
async def test_ensure_agent_session_resumes_over_prewarm():
    """A resume replaces an existing blank pre-warmed session."""
    bridge = _FakeBridge()
    bridge.sessions.add("johnny:sonnet")  # blank pre-warmed
    pm = _process_manager_with(bridge)

    ok = await pm.ensure_agent_session(
        agent_name="johnny", model="sonnet",
        resume_session_id="sess-abc",
    )
    assert ok
    assert bridge.closed == ["johnny:sonnet"]
    assert len(bridge.created) == 1
    assert bridge.created[0]["resume_session_id"] == "sess-abc"
    assert bridge.has_resumed("johnny", "sonnet")


@pytest.mark.asyncio
async def test_ensure_agent_session_noops_when_already_resumed():
    """Once resumed for a key, subsequent calls with a resume_id are no-ops."""
    bridge = _FakeBridge()
    bridge.sessions.add("johnny:sonnet")
    bridge.resumed.add("johnny:sonnet")
    pm = _process_manager_with(bridge)

    ok = await pm.ensure_agent_session(
        agent_name="johnny", model="sonnet",
        resume_session_id="sess-different",
    )
    assert ok
    assert bridge.closed == []
    assert bridge.created == []


@pytest.mark.asyncio
async def test_ensure_agent_session_without_resume_is_unchanged():
    """Existing warm session + no resume_id → fast path, no create."""
    bridge = _FakeBridge()
    bridge.sessions.add("johnny:sonnet")
    pm = _process_manager_with(bridge)

    ok = await pm.ensure_agent_session(agent_name="johnny", model="sonnet")
    assert ok
    assert bridge.closed == []
    assert bridge.created == []


# ── Orchestrator._pick_resume_session ───────────────────────────────────────


def _orchestrator_with(max_age_hours: int = 24):
    from claude_daemon.agents.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.pm = SimpleNamespace(
        config=SimpleNamespace(sdk_resume_max_age_hours=max_age_hours),
    )
    return orch


def test_pick_resume_session_fresh_returns_session_id():
    orch = _orchestrator_with(24)
    now = datetime.now(timezone.utc).isoformat()
    conv = {
        "session_id": "sess-fresh",
        "last_active": now,
        "message_count": 5,
    }
    assert orch._pick_resume_session(conv) == "sess-fresh"


def test_pick_resume_session_stale_returns_none():
    orch = _orchestrator_with(24)
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    conv = {
        "session_id": "sess-stale",
        "last_active": stale,
        "message_count": 5,
    }
    assert orch._pick_resume_session(conv) is None


def test_pick_resume_session_no_session_id_returns_none():
    orch = _orchestrator_with(24)
    now = datetime.now(timezone.utc).isoformat()
    conv = {"session_id": None, "last_active": now, "message_count": 5}
    assert orch._pick_resume_session(conv) is None


def test_pick_resume_session_new_conv_skipped():
    """Brand-new convs (message_count=0) have nothing to resume."""
    orch = _orchestrator_with(24)
    now = datetime.now(timezone.utc).isoformat()
    conv = {"session_id": "sess-new", "last_active": now, "message_count": 0}
    assert orch._pick_resume_session(conv) is None


def test_pick_resume_session_disabled_via_config():
    """max_age_hours = 0 disables the feature entirely."""
    orch = _orchestrator_with(0)
    now = datetime.now(timezone.utc).isoformat()
    conv = {"session_id": "sess-1", "last_active": now, "message_count": 5}
    assert orch._pick_resume_session(conv) is None


def test_pick_resume_session_handles_datetime_object():
    """last_active may be a datetime object (naive or aware)."""
    orch = _orchestrator_with(24)
    conv_aware = {
        "session_id": "sess-1",
        "last_active": datetime.now(timezone.utc),
        "message_count": 1,
    }
    assert orch._pick_resume_session(conv_aware) == "sess-1"

    conv_naive = {
        "session_id": "sess-2",
        "last_active": datetime.utcnow(),
        "message_count": 1,
    }
    assert orch._pick_resume_session(conv_naive) == "sess-2"


def test_pick_resume_session_handles_malformed_timestamp():
    orch = _orchestrator_with(24)
    conv = {
        "session_id": "sess-1",
        "last_active": "not a timestamp",
        "message_count": 1,
    }
    assert orch._pick_resume_session(conv) is None


def test_config_exposes_sdk_resume_max_age_hours():
    from claude_daemon.core.config import DaemonConfig
    cfg = DaemonConfig()
    assert hasattr(cfg, "sdk_resume_max_age_hours")
    assert cfg.sdk_resume_max_age_hours == 24


# ── sessionDead vs transient error behaviour ────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_preserves_session_on_transient_error():
    """A transient error (sessionDead=false) must NOT destroy the session."""
    mgr = _alive_bridge()
    mgr._sessions["johnny:sonnet"] = "sess-1"
    mgr._first_message["johnny:sonnet"] = True

    async def fake_send(cmd):
        req_id = cmd["id"]
        future = mgr._pending.get(req_id)
        if future and not future.done():
            future.set_result({
                "event": "error",
                "id": req_id,
                "message": "overloaded",
                "recoverable": True,
                "sessionDead": False,
            })
    mgr._send_command.side_effect = fake_send

    resp = await mgr.send_message("johnny", "hi", model="sonnet")
    assert resp.is_error
    assert mgr.has_session("johnny", "sonnet"), "session should survive a transient error"
    assert "johnny:sonnet" in mgr._first_message, "_first_message should survive too"


@pytest.mark.asyncio
async def test_send_message_destroys_session_on_dead_error():
    """A dead-session error (sessionDead=true) MUST destroy the session."""
    mgr = _alive_bridge()
    mgr._sessions["johnny:sonnet"] = "sess-1"
    mgr._first_message["johnny:sonnet"] = True

    async def fake_send(cmd):
        req_id = cmd["id"]
        future = mgr._pending.get(req_id)
        if future and not future.done():
            future.set_result({
                "event": "error",
                "id": req_id,
                "message": "session terminated",
                "recoverable": True,
                "sessionDead": True,
            })
    mgr._send_command.side_effect = fake_send

    resp = await mgr.send_message("johnny", "hi", model="sonnet")
    assert resp.is_error
    assert not mgr.has_session("johnny", "sonnet"), "session should be removed when dead"
    assert "johnny:sonnet" not in mgr._first_message


@pytest.mark.asyncio
async def test_stream_message_preserves_session_on_transient_error():
    """Streaming: transient error preserves session."""
    mgr = _alive_bridge()
    mgr._sessions["johnny:sonnet"] = "sess-1"
    mgr._first_message["johnny:sonnet"] = True

    async def feeder():
        for _ in range(10):
            if mgr._streams:
                break
            await asyncio.sleep(0.005)
        queue = next(iter(mgr._streams.values()))
        await queue.put({
            "event": "error",
            "message": "rate limited",
            "recoverable": True,
            "sessionDead": False,
        })

    task = asyncio.create_task(feeder())
    chunks = []
    async for item in mgr.stream_message("johnny", "hi", model="sonnet"):
        chunks.append(item)
    await task

    assert any(isinstance(c, ClaudeResponse) and c.is_error for c in chunks)
    assert mgr.has_session("johnny", "sonnet"), "session should survive transient error"


@pytest.mark.asyncio
async def test_stream_message_destroys_session_on_dead_error():
    """Streaming: dead session error removes session."""
    mgr = _alive_bridge()
    mgr._sessions["johnny:sonnet"] = "sess-1"
    mgr._first_message["johnny:sonnet"] = True

    async def feeder():
        for _ in range(10):
            if mgr._streams:
                break
            await asyncio.sleep(0.005)
        queue = next(iter(mgr._streams.values()))
        await queue.put({
            "event": "error",
            "message": "session closed",
            "recoverable": True,
            "sessionDead": True,
        })

    task = asyncio.create_task(feeder())
    chunks = []
    async for item in mgr.stream_message("johnny", "hi", model="sonnet"):
        chunks.append(item)
    await task

    assert any(isinstance(c, ClaudeResponse) and c.is_error for c in chunks)
    assert not mgr.has_session("johnny", "sonnet"), "dead session should be removed"


# ── Server-reported dead sessions (regression: bridge.js now detects these) ─


@pytest.mark.asyncio
async def test_send_message_pops_session_when_server_reports_dead():
    """bridge.js flags 'No conversation found with session ID' as sessionDead=true.
    Python must pop the session so the next message creates a fresh one.
    """
    mgr = _alive_bridge()
    mgr._sessions["johnny:sonnet"] = "bad-id-00000000"
    mgr._first_message["johnny:sonnet"] = True

    async def fake_send(cmd):
        req_id = cmd["id"]
        future = mgr._pending.get(req_id)
        if future and not future.done():
            future.set_result({
                "event": "error",
                "id": req_id,
                "message": (
                    "Claude Code returned an error result: "
                    "No conversation found with session ID: bad-id-00000000"
                ),
                "recoverable": True,
                "sessionDead": True,
            })
    mgr._send_command.side_effect = fake_send

    resp = await mgr.send_message("johnny", "hi", model="sonnet")
    assert resp.is_error
    assert "No conversation found" in resp.result
    assert not mgr.has_session("johnny", "sonnet")
    assert "johnny:sonnet" not in mgr._first_message


@pytest.mark.asyncio
async def test_stream_message_pops_session_when_server_reports_dead():
    """Streaming path mirrors the buffered-path behaviour for server-dead errors."""
    mgr = _alive_bridge()
    mgr._sessions["johnny:sonnet"] = "bad-id-00000000"
    mgr._first_message["johnny:sonnet"] = True

    async def feeder():
        for _ in range(10):
            if mgr._streams:
                break
            await asyncio.sleep(0.005)
        queue = next(iter(mgr._streams.values()))
        await queue.put({
            "event": "error",
            "message": (
                "Claude Code returned an error result: "
                "No conversation found with session ID: bad-id-00000000"
            ),
            "recoverable": True,
            "sessionDead": True,
        })

    task = asyncio.create_task(feeder())
    chunks = []
    async for item in mgr.stream_message("johnny", "hi", model="sonnet"):
        chunks.append(item)
    await task

    errors = [c for c in chunks if isinstance(c, ClaudeResponse) and c.is_error]
    assert errors
    assert "No conversation found" in errors[0].result
    assert not mgr.has_session("johnny", "sonnet")
    assert "johnny:sonnet" not in mgr._first_message
