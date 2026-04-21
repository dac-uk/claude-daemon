"""Tests for stream_message error surfacing (Phase 9).

These cover the path where the Claude CLI subprocess exits with no output —
the most common real-world failure when ANTHROPIC_API_KEY is missing or the
CLI auth has expired. Previously, the stream silently emitted an empty error;
now stderr is captured and propagated.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_daemon.agents.orchestrator import Orchestrator
from claude_daemon.core.process import ClaudeResponse, ProcessManager


def _make_pm() -> ProcessManager:
    config = MagicMock()
    config.max_concurrent_sessions = 5
    config.max_budget_per_message = 0.5
    config.claude_binary = "claude"
    config.permission_mode = "auto"
    config.default_model = None
    config.mcp_config = None
    config.stream_idle_timeout_ms = 300000
    config.auto_compact_pct = 70
    config.managed_agents_enabled = False
    config.managed_agents_task_types = []
    pm = ProcessManager(config)
    # Disable optional bridges so we go straight to the subprocess path.
    pm._sdk_bridge = None
    pm._managed = None
    return pm


class _FakeStdout:
    """Async-iterable stdout that yields the given lines then stops."""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeStderr:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self) -> bytes:
        data, self._data = self._data, b""
        return data


class _FakeProc:
    def __init__(self, stdout_lines: list[bytes], stderr: bytes, returncode: int):
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStderr(stderr)
        self.returncode = returncode

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_stream_message_with_empty_stdout_surfaces_stderr():
    """When the CLI exits without output, stderr is included in the yielded error."""
    pm = _make_pm()
    proc = _FakeProc(
        stdout_lines=[],
        stderr=b"ANTHROPIC_API_KEY not set\n",
        returncode=1,
    )

    async def fake_exec(*args, **kwargs):
        return proc

    chunks: list = []
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        async for chunk in pm.stream_message(prompt="hi"):
            chunks.append(chunk)

    assert len(chunks) == 1
    resp = chunks[0]
    assert isinstance(resp, ClaudeResponse)
    assert resp.is_error is True
    assert "ANTHROPIC_API_KEY not set" in resp.result


@pytest.mark.asyncio
async def test_stream_message_with_empty_stdout_no_stderr_uses_exit_code():
    """With empty stdout AND empty stderr, the error mentions the exit code."""
    pm = _make_pm()
    proc = _FakeProc(stdout_lines=[], stderr=b"", returncode=2)

    async def fake_exec(*args, **kwargs):
        return proc

    chunks: list = []
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        async for chunk in pm.stream_message(prompt="hi"):
            chunks.append(chunk)

    resp = chunks[0]
    assert isinstance(resp, ClaudeResponse)
    assert resp.is_error is True
    assert "exit" in resp.result.lower() or "2" in resp.result
    assert "ANTHROPIC_API_KEY" in resp.result  # Always mentioned as a hint


@pytest.mark.asyncio
async def test_stream_message_content_but_no_result_event_not_marked_error():
    """Partial stream (assistant deltas but no `result` event) is still a success."""
    pm = _make_pm()
    assistant_line = (
        b'{"type":"assistant","message":{"content":"hello world"}}\n'
    )
    proc = _FakeProc(stdout_lines=[assistant_line], stderr=b"", returncode=0)

    async def fake_exec(*args, **kwargs):
        return proc

    chunks: list = []
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        async for chunk in pm.stream_message(prompt="hi"):
            chunks.append(chunk)

    text_chunks = [c for c in chunks if isinstance(c, str)]
    final = [c for c in chunks if isinstance(c, ClaudeResponse)]
    assert text_chunks == ["hello world"]
    assert len(final) == 1
    assert final[0].is_error is False
    assert final[0].result == "hello world"


# -- Orchestrator-level warning on empty response ----------------------


def _make_agent(name: str = "albert") -> MagicMock:
    agent = MagicMock()
    agent.name = name
    agent.workspace = "/tmp/workspace"
    agent.mcp_config_path = None
    agent.settings_path = None
    agent.build_system_context.return_value = "system"
    agent.build_dynamic_context.return_value = "dynamic"
    agent.build_static_context.return_value = "static"
    agent.get_model.return_value = "sonnet"
    agent.get_effort.return_value = None
    return agent


@pytest.mark.asyncio
async def test_stream_to_agent_logs_warning_on_empty_response(caplog):
    """When the process manager yields an error response, a WARNING is emitted."""
    registry = MagicMock()
    registry.get_agent_summary.return_value = "agents: albert"

    pm = MagicMock()
    pm.ensure_agent_session = AsyncMock()
    pm._sdk_bridge = None
    pm.config.embedding_search_timeout_ms = 400
    pm.config.embedding_interactive_chat = True
    pm.config.sdk_resume_max_age_hours = 24

    async def fake_stream(*args, **kwargs):
        yield ClaudeResponse.error("ANTHROPIC_API_KEY not set")

    pm.stream_message = fake_stream

    store = MagicMock()
    store.get_or_create_conversation.return_value = {
        "id": 1, "session_id": "s1", "message_count": 0, "last_active": None,
    }

    orch = Orchestrator(registry=registry, process_manager=pm, store=store, hub=None)
    orch._semantic_search = AsyncMock(return_value=[])

    agent = _make_agent()

    caplog.set_level(logging.WARNING, logger="claude_daemon.agents.orchestrator")

    chunks = []
    async for chunk in orch.stream_to_agent(agent=agent, prompt="hello"):
        chunks.append(chunk)

    # Final chunk is the error response.
    assert any(isinstance(c, ClaudeResponse) and c.is_error for c in chunks)
    # The warning log fired with the expected details.
    warnings = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "produced no usable response" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "albert" in warnings[0].getMessage()


# -- SDK bridge failure → CLI fallback --


@pytest.mark.asyncio
async def test_sdk_bridge_error_falls_back_to_cli():
    """When the SDK bridge yields an error, pm.stream_message falls through to CLI."""
    pm = _make_pm()

    # Set up a fake SDK bridge that has a session but yields an error
    bridge = MagicMock()
    bridge.has_session.return_value = True

    async def failing_sdk_stream(**kwargs):
        yield ClaudeResponse.error("SDK bridge: stream ended without result")

    bridge.stream_message = failing_sdk_stream
    bridge._key = lambda a, m: f"{a}:{m}"
    bridge._sessions = {"albert:sonnet": "warm-session-123"}
    pm._sdk_bridge = bridge
    pm.config.default_model = "sonnet"

    # Set up CLI subprocess to produce a real response
    result_line = (
        b'{"type":"result","subtype":"success","is_error":false,'
        b'"result":"Hello from CLI!","session_id":"cli-123",'
        b'"total_cost_usd":0.01,"num_turns":1,"duration_ms":500,'
        b'"usage":{"input_tokens":10,"output_tokens":5}}\n'
    )
    proc = _FakeProc(stdout_lines=[result_line], stderr=b"", returncode=0)

    async def fake_exec(*args, **kwargs):
        return proc

    chunks: list = []
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        async for chunk in pm.stream_message(prompt="hi", agent_name="albert"):
            chunks.append(chunk)

    # The CLI fallback should produce the real response
    final = [c for c in chunks if isinstance(c, ClaudeResponse)]
    assert len(final) == 1
    assert final[0].is_error is False
    assert final[0].result == "Hello from CLI!"
    # SDK session is preserved — the bridge handles its own cleanup for
    # truly dead sessions (recoverable errors). Keeping the session lets
    # the next message retry via SDK instead of falling through to CLI.
    assert "albert:sonnet" in bridge._sessions


@pytest.mark.asyncio
async def test_sdk_bridge_empty_stream_falls_back_to_cli():
    """When the SDK bridge yields nothing (shouldn't happen after fix), CLI is used."""
    pm = _make_pm()

    bridge = MagicMock()
    bridge.has_session.return_value = True

    async def empty_sdk_stream(**kwargs):
        return
        yield  # make it a generator

    bridge.stream_message = empty_sdk_stream
    bridge._key = lambda a, m: f"{a}:{m}"
    bridge._sessions = {"albert:sonnet": "warm-session-123"}
    pm._sdk_bridge = bridge
    pm.config.default_model = "sonnet"

    result_line = (
        b'{"type":"result","subtype":"success","is_error":false,'
        b'"result":"CLI fallback response","session_id":"cli-456",'
        b'"total_cost_usd":0.01,"num_turns":1,"duration_ms":200,'
        b'"usage":{"input_tokens":5,"output_tokens":3}}\n'
    )
    proc = _FakeProc(stdout_lines=[result_line], stderr=b"", returncode=0)

    async def fake_exec(*args, **kwargs):
        return proc

    chunks: list = []
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        async for chunk in pm.stream_message(prompt="hi", agent_name="albert"):
            chunks.append(chunk)

    final = [c for c in chunks if isinstance(c, ClaudeResponse)]
    assert len(final) == 1
    assert final[0].result == "CLI fallback response"
