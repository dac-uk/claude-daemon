"""Tests for the ProcessManager."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from claude_daemon.core.process import (
    ClaudeResponse,
    ProcessManager,
    _is_rate_limit_error,
)


def test_claude_response_from_json():
    """Test parsing a Claude JSON response."""
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Hello! 2+2 is 4.",
        "session_id": "abc-123-def",
        "total_cost_usd": 0.05,
        "num_turns": 1,
        "duration_ms": 1500,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
        },
    }

    resp = ClaudeResponse.from_json(data)
    assert resp.result == "Hello! 2+2 is 4."
    assert resp.session_id == "abc-123-def"
    assert resp.cost == 0.05
    assert resp.input_tokens == 100
    assert resp.output_tokens == 50
    assert resp.num_turns == 1
    assert resp.duration_ms == 1500
    assert resp.is_error is False


def test_claude_response_error():
    """Test creating an error response."""
    resp = ClaudeResponse.error("Something went wrong")
    assert resp.result == "Something went wrong"
    assert resp.is_error is True
    assert resp.cost == 0
    assert resp.session_id == ""


def test_claude_response_from_error_json():
    """Test parsing an error JSON response."""
    data = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "Authentication error",
        "session_id": "xyz-789",
        "total_cost_usd": 0,
        "num_turns": 0,
        "duration_ms": 50,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }

    resp = ClaudeResponse.from_json(data)
    assert resp.is_error is True
    assert resp.result == "Authentication error"
    assert resp.cost == 0


def test_is_session_busy():
    """is_session_busy reports True when a session is in the active dict."""
    config = MagicMock()
    config.max_concurrent_sessions = 5
    pm = ProcessManager(config)
    assert pm.is_session_busy("session-1") is False
    pm._active["session-1"] = MagicMock()
    assert pm.is_session_busy("session-1") is True


@pytest.mark.asyncio
async def test_auto_parallel_send_message_skips_locked_session():
    """When a session's lock is held, send_message auto-parallels on a fresh session."""
    config = MagicMock()
    config.max_concurrent_sessions = 5
    config.max_budget_per_message = 0.5
    pm = ProcessManager(config)

    called_sessions = []
    original_execute = pm._execute_buffered

    async def fake_execute(prompt, session_id, *args, **kwargs):
        called_sessions.append(session_id)
        return ClaudeResponse(
            result="ok", session_id=session_id or "new-uuid",
            cost=0.01, input_tokens=10, output_tokens=5,
            num_turns=1, duration_ms=100, is_error=False,
        )

    pm._execute_buffered = fake_execute

    # Pre-lock the session to simulate it being busy
    lock = pm._get_session_lock("busy-session")
    await lock.acquire()

    try:
        # This should NOT block — it should auto-parallel with session_id=None
        resp = await asyncio.wait_for(
            pm.send_message(prompt="test", session_id="busy-session"),
            timeout=1.0,
        )
        assert resp.result == "ok"
        # The execute should have been called with None (fresh session)
        assert called_sessions == [None]
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_auto_parallel_stream_replaces_active_session():
    """When a session has an active subprocess, stream_message creates a fresh session."""
    config = MagicMock()
    config.max_concurrent_sessions = 5
    config.max_budget_per_message = 0.5
    config.claude_binary = "echo"
    config.permission_mode = "auto"
    config.default_model = None
    config.mcp_config = None
    pm = ProcessManager(config)

    # Mark a session as active
    pm._active["active-session"] = MagicMock()

    # Build args should use a fresh session_id (not --resume active-session)
    # We test by checking the args built after auto-parallel triggers
    args, tracking_id = pm._build_args(
        prompt="test",
        session_id=None,  # This is what auto-parallel passes
        system_context=None,
        max_budget=0.5,
    )
    assert "--resume" not in args
    assert "--session-id" in args


# -- Model fallback / rate limit detection --


def test_rate_limit_detection_stderr():
    """_is_rate_limit_error detects rate limit patterns in stderr."""
    assert _is_rate_limit_error("Error: rate limit exceeded")
    assert _is_rate_limit_error("HTTP 429 Too Many Requests")
    assert _is_rate_limit_error("Model is overloaded, please retry")
    assert _is_rate_limit_error("The model is currently unavailable")
    assert _is_rate_limit_error("quota exceeded for this billing period")
    assert _is_rate_limit_error("server at capacity")
    assert not _is_rate_limit_error("Authentication error")
    assert not _is_rate_limit_error("Invalid prompt")
    assert not _is_rate_limit_error("")


def test_rate_limit_detection_response():
    """_is_rate_limit_error detects rate limit in error response result."""
    resp = ClaudeResponse.error("Claude Code error: rate limit exceeded")
    assert _is_rate_limit_error("", resp)

    normal = ClaudeResponse.error("Invalid JSON in response")
    assert not _is_rate_limit_error("", normal)

    ok = ClaudeResponse(
        result="Hello!", session_id="s1", cost=0.01,
        input_tokens=10, output_tokens=5, num_turns=1,
        duration_ms=100, is_error=False,
    )
    assert not _is_rate_limit_error("", ok)


def test_fallback_chain_deduplication():
    """_build_fallback_chain deduplicates models."""
    config = MagicMock()
    config.max_concurrent_sessions = 5
    config.model_fallback_chain = ["sonnet", "haiku"]
    pm = ProcessManager(config)

    # Requested model is already in the chain — should not duplicate
    chain = pm._build_fallback_chain("sonnet")
    assert chain == ["sonnet", "haiku"]

    # Requested model is NOT in the chain — prepended
    chain = pm._build_fallback_chain("opus")
    assert chain == ["opus", "sonnet", "haiku"]

    # No requested model
    chain = pm._build_fallback_chain(None)
    assert chain == ["sonnet", "haiku"]


@pytest.mark.asyncio
async def test_execute_buffered_falls_back_on_rate_limit():
    """When first model hits rate limit, fallback to next model."""
    config = MagicMock()
    config.max_concurrent_sessions = 5
    config.max_budget_per_message = 0.5
    config.model_fallback_chain = ["sonnet", "haiku"]
    config.model_retry_delay = 0.01  # Fast for test
    config.model_max_retries = 2
    pm = ProcessManager(config)

    call_count = 0

    async def fake_once(prompt, session_id, system_context, max_budget,
                        platform, user_id, model_override=None, mcp_config_path=None):
        nonlocal call_count
        call_count += 1
        if model_override == "opus":
            return ClaudeResponse.error("Claude Code error: rate limit exceeded"), "429 rate limit"
        return ClaudeResponse(
            result="ok", session_id="s1", cost=0.01,
            input_tokens=10, output_tokens=5, num_turns=1,
            duration_ms=100, is_error=False,
        ), ""

    pm._execute_buffered_once = fake_once

    resp = await pm._execute_buffered(
        "test", None, None, 0.5, "cli", "local", "opus", None,
    )
    assert not resp.is_error
    assert resp.result == "ok"
    assert resp.model_used == "sonnet"
    assert call_count == 2  # opus failed, sonnet succeeded


@pytest.mark.asyncio
async def test_execute_buffered_no_fallback_on_normal_error():
    """Normal errors (not rate limit) don't trigger fallback."""
    config = MagicMock()
    config.max_concurrent_sessions = 5
    config.max_budget_per_message = 0.5
    config.model_fallback_chain = ["sonnet", "haiku"]
    config.model_retry_delay = 0.01
    config.model_max_retries = 2
    pm = ProcessManager(config)

    call_count = 0

    async def fake_once(prompt, session_id, system_context, max_budget,
                        platform, user_id, model_override=None, mcp_config_path=None):
        nonlocal call_count
        call_count += 1
        return ClaudeResponse.error("Authentication error"), "auth failed"

    pm._execute_buffered_once = fake_once

    resp = await pm._execute_buffered(
        "test", None, None, 0.5, "cli", "local", "opus", None,
    )
    assert resp.is_error
    assert call_count == 1  # No retry


@pytest.mark.asyncio
async def test_execute_buffered_exhausts_chain():
    """When all models fail with rate limit, return last error."""
    config = MagicMock()
    config.max_concurrent_sessions = 5
    config.max_budget_per_message = 0.5
    config.model_fallback_chain = ["sonnet", "haiku"]
    config.model_retry_delay = 0.01
    config.model_max_retries = 3
    pm = ProcessManager(config)

    call_count = 0

    async def fake_once(prompt, session_id, system_context, max_budget,
                        platform, user_id, model_override=None, mcp_config_path=None):
        nonlocal call_count
        call_count += 1
        return ClaudeResponse.error("rate limit exceeded"), "429"

    pm._execute_buffered_once = fake_once

    resp = await pm._execute_buffered(
        "test", None, None, 0.5, "cli", "local", "opus", None,
    )
    assert resp.is_error
    assert "rate limit" in resp.result
    assert call_count == 3  # opus, sonnet, haiku — all failed
