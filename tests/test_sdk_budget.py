"""Tests for SDK bridge budget enforcement (Bug 1: SDK bridge bypasses max_budget).

The CLI subprocess path enforces --max-budget-usd at turn start.  The SDK
bridge path had no equivalent — cost is only knowable *after* the turn
completes, so we do post-hoc detection: the bridge includes `budgetExceeded`
in the result event and Python logs at ERROR level.

These tests verify:
  1. `send_message` passes `maxBudget` in the bridge command.
  2. `stream_message` passes `maxBudget` in the bridge command.
  3. When the result has `budgetExceeded=True`, Python logs at ERROR.
  4. When cost > max_budget (even without the flag), Python logs at ERROR.
  5. When cost is within budget, no ERROR is logged.
  6. process.py passes `budget` to both send_message and stream_message bridge calls.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_daemon.core.process import ClaudeResponse
from claude_daemon.core.sdk_bridge import SDKBridgeManager


# ── Helpers ───────────────────────────────────────────────────────────────────


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        sdk_bridge_idle_timeout_ms=5_000,
        process_timeout=60,
        sdk_bridge_node_path="node",
    )


def _make_bridge() -> SDKBridgeManager:
    mgr = SDKBridgeManager(_config())
    mgr._send_command = AsyncMock()
    return mgr


def _inject_result(mgr: SDKBridgeManager, result_event: dict) -> None:
    """Inject a single result event into the most-recently-registered queue."""

    async def _feed():
        # Wait until stream_message has registered its queue
        for _ in range(50):
            if mgr._streams:
                break
            await asyncio.sleep(0.005)
        queue = next(iter(mgr._streams.values()))
        await queue.put(result_event)
        await queue.put(None)  # signal end

    asyncio.get_event_loop().call_soon(lambda: asyncio.ensure_future(_feed()))


def _inject_pending_result(mgr: SDKBridgeManager, result_event: dict) -> None:
    """Inject result event into the most-recently-registered pending future."""

    async def _feed():
        for _ in range(50):
            if mgr._pending:
                break
            await asyncio.sleep(0.005)
        # Find the non-__ready__ pending future
        for req_id, future in mgr._pending.items():
            if req_id != "__ready__" and not future.done():
                future.set_result(result_event)
                return

    asyncio.get_event_loop().call_soon(lambda: asyncio.ensure_future(_feed()))


# ── send_message tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_passes_max_budget_in_command():
    """send_message includes maxBudget in the bridge command when set."""
    mgr = _make_bridge()
    mgr._sessions["johnny:sonnet"] = "sess1"
    mgr._first_message["johnny:sonnet"] = True  # skip system-prompt injection

    result_event = {
        "event": "result", "id": None, "agent": "johnny:sonnet",
        "sessionId": "sess1", "result": "ok", "cost": 0.05,
        "inputTokens": 10, "outputTokens": 5, "durationMs": 100,
        "stopReason": "end_turn", "budgetExceeded": False,
    }
    _inject_pending_result(mgr, result_event)

    await mgr.send_message("johnny", "hello", model="sonnet", max_budget=0.50)

    call_args = mgr._send_command.call_args[0][0]
    assert call_args["cmd"] == "send"
    assert call_args.get("maxBudget") == 0.50


@pytest.mark.asyncio
async def test_send_message_no_max_budget_field_when_none():
    """When max_budget is None, maxBudget must NOT appear in bridge command."""
    mgr = _make_bridge()
    mgr._sessions["johnny:sonnet"] = "sess1"
    mgr._first_message["johnny:sonnet"] = True

    result_event = {
        "event": "result", "id": None, "agent": "johnny:sonnet",
        "sessionId": "sess1", "result": "ok", "cost": 0.01,
        "inputTokens": 5, "outputTokens": 2, "durationMs": 50,
        "stopReason": "end_turn", "budgetExceeded": False,
    }
    _inject_pending_result(mgr, result_event)

    await mgr.send_message("johnny", "hello", model="sonnet", max_budget=None)

    call_args = mgr._send_command.call_args[0][0]
    assert "maxBudget" not in call_args


@pytest.mark.asyncio
async def test_send_message_logs_error_on_budget_exceeded_flag(caplog):
    """If bridge reports budgetExceeded=True, Python logs at ERROR level."""
    mgr = _make_bridge()
    mgr._sessions["johnny:sonnet"] = "sess1"
    mgr._first_message["johnny:sonnet"] = True

    result_event = {
        "event": "result", "id": None, "agent": "johnny:sonnet",
        "sessionId": "sess1", "result": "long reply",
        "cost": 1.50,  # blew the budget
        "inputTokens": 5000, "outputTokens": 2000, "durationMs": 30_000,
        "stopReason": "end_turn", "budgetExceeded": True,
    }
    _inject_pending_result(mgr, result_event)

    with caplog.at_level(logging.ERROR, logger="claude_daemon.core.sdk_bridge"):
        resp = await mgr.send_message("johnny", "long task", model="sonnet", max_budget=0.50)

    assert not resp.is_error
    assert resp.cost == 1.50
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "Expected an ERROR log for budget exceeded"
    assert "budget exceeded" in error_records[0].message.lower()


@pytest.mark.asyncio
async def test_send_message_logs_error_when_cost_exceeds_budget(caplog):
    """Even without budgetExceeded flag, Python checks cost vs max_budget."""
    mgr = _make_bridge()
    mgr._sessions["johnny:sonnet"] = "sess1"
    mgr._first_message["johnny:sonnet"] = True

    result_event = {
        "event": "result", "id": None, "agent": "johnny:sonnet",
        "sessionId": "sess1", "result": "ok", "cost": 2.00,
        "inputTokens": 1000, "outputTokens": 500, "durationMs": 5_000,
        "stopReason": "end_turn", "budgetExceeded": False,  # bridge flag absent/false
    }
    _inject_pending_result(mgr, result_event)

    with caplog.at_level(logging.ERROR, logger="claude_daemon.core.sdk_bridge"):
        resp = await mgr.send_message("johnny", "task", model="sonnet", max_budget=0.50)

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "Expected an ERROR log when cost > max_budget"
    assert resp.cost == 2.00


@pytest.mark.asyncio
async def test_send_message_no_error_log_within_budget(caplog):
    """No ERROR logged when cost is within budget."""
    mgr = _make_bridge()
    mgr._sessions["johnny:sonnet"] = "sess1"
    mgr._first_message["johnny:sonnet"] = True

    result_event = {
        "event": "result", "id": None, "agent": "johnny:sonnet",
        "sessionId": "sess1", "result": "ok", "cost": 0.10,
        "inputTokens": 100, "outputTokens": 50, "durationMs": 1_000,
        "stopReason": "end_turn", "budgetExceeded": False,
    }
    _inject_pending_result(mgr, result_event)

    with caplog.at_level(logging.ERROR, logger="claude_daemon.core.sdk_bridge"):
        await mgr.send_message("johnny", "task", model="sonnet", max_budget=0.50)

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not error_records, f"Unexpected ERROR log: {error_records}"


# ── stream_message tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_message_passes_max_budget_in_command():
    """stream_message includes maxBudget in the bridge command when set."""
    mgr = _make_bridge()
    mgr._sessions["johnny:sonnet"] = "sess1"
    mgr._first_message["johnny:sonnet"] = True

    result_event = {
        "event": "result", "sessionId": "sess1", "result": "ok",
        "cost": 0.05, "inputTokens": 10, "outputTokens": 5,
        "durationMs": 100, "stopReason": "end_turn", "budgetExceeded": False,
    }
    _inject_result(mgr, result_event)

    chunks = []
    async for item in mgr.stream_message("johnny", "hi", model="sonnet", max_budget=1.00):
        chunks.append(item)

    call_args = mgr._send_command.call_args[0][0]
    assert call_args["cmd"] == "send"
    assert call_args.get("maxBudget") == 1.00


@pytest.mark.asyncio
async def test_stream_message_logs_error_on_budget_exceeded(caplog):
    """stream_message logs ERROR when budgetExceeded=True in result."""
    mgr = _make_bridge()
    mgr._sessions["johnny:sonnet"] = "sess1"
    mgr._first_message["johnny:sonnet"] = True

    result_event = {
        "event": "result", "sessionId": "sess1", "result": "long",
        "cost": 3.00, "inputTokens": 5000, "outputTokens": 3000,
        "durationMs": 60_000, "stopReason": "end_turn", "budgetExceeded": True,
    }
    _inject_result(mgr, result_event)

    with caplog.at_level(logging.ERROR, logger="claude_daemon.core.sdk_bridge"):
        async for _ in mgr.stream_message("johnny", "task", model="sonnet", max_budget=0.50):
            pass

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "Expected ERROR log for budget exceeded in stream"
    assert "budget exceeded" in error_records[0].message.lower()


@pytest.mark.asyncio
async def test_stream_message_no_error_within_budget(caplog):
    """No ERROR logged when stream cost is within budget."""
    mgr = _make_bridge()
    mgr._sessions["johnny:sonnet"] = "sess1"
    mgr._first_message["johnny:sonnet"] = True

    result_event = {
        "event": "result", "sessionId": "sess1", "result": "ok",
        "cost": 0.05, "inputTokens": 100, "outputTokens": 50,
        "durationMs": 1_000, "stopReason": "end_turn", "budgetExceeded": False,
    }
    _inject_result(mgr, result_event)

    with caplog.at_level(logging.ERROR, logger="claude_daemon.core.sdk_bridge"):
        async for _ in mgr.stream_message("johnny", "hi", model="sonnet", max_budget=1.00):
            pass

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not error_records, f"Unexpected ERROR: {error_records}"


# ── process.py integration tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_send_message_passes_budget_to_bridge():
    """ProcessManager.send_message forwards budget to sdk_bridge.send_message."""
    from claude_daemon.core.process import ProcessManager

    config = MagicMock()
    config.max_concurrent_sessions = 2
    config.max_budget_per_message = 0.75
    config.default_model = "sonnet"
    config.permission_mode = "auto"

    pm = ProcessManager(config)
    pm._managed = None

    bridge = MagicMock()
    bridge.has_session.return_value = True
    captured: dict = {}

    async def fake_send(agent_name, prompt, context, model, max_budget):
        captured["max_budget"] = max_budget
        return ClaudeResponse(
            result="hello", session_id="s1", cost=0.10,
            input_tokens=10, output_tokens=5, num_turns=1,
            duration_ms=500, is_error=False, stop_reason="end_turn",
        )

    bridge.send_message = fake_send
    pm._sdk_bridge = bridge

    await pm.send_message("hi", agent_name="johnny", max_budget=0.75)
    assert captured["max_budget"] == 0.75


@pytest.mark.asyncio
async def test_process_stream_message_passes_budget_to_bridge():
    """ProcessManager.stream_message forwards budget to sdk_bridge.stream_message."""
    from claude_daemon.core.process import ProcessManager

    config = MagicMock()
    config.max_concurrent_sessions = 2
    config.max_budget_per_message = 0.50
    config.default_model = "sonnet"
    config.permission_mode = "auto"

    pm = ProcessManager(config)
    pm._managed = None

    bridge = MagicMock()
    bridge.has_session.return_value = True
    captured: dict = {}

    async def fake_stream(agent_name, prompt, context, model, max_budget):
        captured["max_budget"] = max_budget
        yield ClaudeResponse(
            result="hi", session_id="s1", cost=0.05,
            input_tokens=5, output_tokens=2, num_turns=1,
            duration_ms=200, is_error=False, stop_reason="end_turn",
        )

    bridge.stream_message = fake_stream
    pm._sdk_bridge = bridge

    chunks = []
    async for chunk in pm.stream_message("hi", agent_name="johnny", max_budget=0.50):
        chunks.append(chunk)

    assert captured["max_budget"] == 0.50


# ── pre-warm chat_model tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prewarm_includes_chat_model_when_different(tmp_path):
    """Pre-warm creates a session for chat_model if it differs from default_model."""
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.core.daemon import ClaudeDaemon

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    reg = AgentRegistry(agents_dir)
    # Create an agent and patch its models so chat != default
    reg.create_agent("alice", role="test")
    alice = reg.get("alice")
    alice.identity.default_model = "haiku"
    alice.identity.chat_model = "sonnet"   # different — must be warmed
    alice.identity.planning_model = "haiku"  # same as default — not added again

    config = DaemonConfig(data_dir=tmp_path, sdk_prewarm_concurrency=4)
    daemon = ClaudeDaemon.__new__(ClaudeDaemon)
    daemon.config = config
    daemon.agent_registry = reg

    warmed_models: list[str] = []

    async def mock_ensure(agent_name, model, **kwargs):
        warmed_models.append(model)
        return "sess_id"

    bridge = MagicMock()
    bridge.is_alive = True
    pm = MagicMock()
    pm._sdk_bridge = bridge
    pm.ensure_agent_session = AsyncMock(side_effect=mock_ensure)
    daemon.process_manager = pm

    await daemon._precreate_agent_sessions()

    assert "sonnet" in warmed_models, "chat_model 'sonnet' should have been warmed"
    assert "haiku" in warmed_models, "default_model 'haiku' should have been warmed"


@pytest.mark.asyncio
async def test_prewarm_does_not_duplicate_when_chat_equals_default(tmp_path):
    """When chat_model == default_model, only one session is created for that model."""
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.core.daemon import ClaudeDaemon

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    reg = AgentRegistry(agents_dir)
    reg.create_agent("bob", role="test")
    bob = reg.get("bob")
    bob.identity.default_model = "sonnet"
    bob.identity.chat_model = "sonnet"  # same as default — no extra session
    bob.identity.planning_model = "sonnet"  # same too

    config = DaemonConfig(data_dir=tmp_path, sdk_prewarm_concurrency=4)
    daemon = ClaudeDaemon.__new__(ClaudeDaemon)
    daemon.config = config
    daemon.agent_registry = reg

    warmed_models: list[str] = []

    async def mock_ensure(agent_name, model, **kwargs):
        warmed_models.append(model)
        return "sess_id"

    bridge = MagicMock()
    bridge.is_alive = True
    pm = MagicMock()
    pm._sdk_bridge = bridge
    pm.ensure_agent_session = AsyncMock(side_effect=mock_ensure)
    daemon.process_manager = pm

    await daemon._precreate_agent_sessions()

    # Only 1 session for bob (all models are sonnet)
    assert warmed_models.count("sonnet") == 1
    assert len(warmed_models) == 1
