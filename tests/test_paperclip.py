"""Tests for Paperclip integration — polling, heartbeat, cost reporting."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_daemon.integrations.paperclip import PaperclipIntegration


@pytest.fixture
def pc():
    """Create a PaperclipIntegration for testing (not started)."""
    return PaperclipIntegration(
        url="https://paperclip.example.com",
        api_key="test-key",
        poll_interval=1,
        task_limit=3,
        startup_timeout=5,
    )


# ------------------------------------------------------------------ #
# Constructor and config
# ------------------------------------------------------------------ #


def test_config_defaults():
    pc = PaperclipIntegration(url="https://example.com", api_key="k")
    assert pc.poll_interval == 5
    assert pc.task_limit == 5
    assert pc.startup_timeout == 30
    assert pc.url == "https://example.com"


def test_url_trailing_slash_stripped():
    pc = PaperclipIntegration(url="https://example.com/api/", api_key="k")
    assert pc.url == "https://example.com/api"


# ------------------------------------------------------------------ #
# Registration with retry
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_register_success(pc):
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    pc._client = mock_client

    await pc._register_with_retry()

    assert pc._registered is True
    mock_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_register_retries_on_failure(pc):
    fail_resp = MagicMock()
    fail_resp.status_code = 500
    fail_resp.text = "Server error"

    success_resp = MagicMock()
    success_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=[fail_resp, success_resp])
    pc._client = mock_client

    await pc._register_with_retry()

    assert pc._registered is True
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_register_fails_gracefully(pc):
    """After max retries, integration continues without registration."""
    fail_resp = MagicMock()
    fail_resp.status_code = 500
    fail_resp.text = "Server error"

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=fail_resp)
    pc._client = mock_client

    await pc._register_with_retry()

    assert pc._registered is False
    assert mock_client.post.call_count == 3  # _REGISTER_MAX_RETRIES


# ------------------------------------------------------------------ #
# send_response with cost data
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_send_response_includes_cost(pc):
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    pc._client = mock_client

    await pc.send_response(
        channel_id="task-123",
        content="Done!",
        task_id="task-123",
        agent_name="albert",
        cost=0.15,
        input_tokens=1000,
        output_tokens=500,
    )

    call_args = mock_client.post.call_args
    payload = call_args.kwargs["json"]
    assert payload["result"] == "Done!"
    assert payload["agent"] == "albert"
    assert payload["cost_usd"] == 0.15
    assert payload["input_tokens"] == 1000
    assert payload["output_tokens"] == 500


@pytest.mark.asyncio
async def test_send_response_without_cost(pc):
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    pc._client = mock_client

    await pc.send_response(channel_id="task-456", content="Result")

    payload = mock_client.post.call_args.kwargs["json"]
    assert "cost_usd" not in payload
    assert payload["agent"] == "claude-daemon"


# ------------------------------------------------------------------ #
# Heartbeat handler
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_heartbeat_routes_task(pc):
    handler = AsyncMock()
    pc.set_message_handler(handler)

    result = await pc.handle_heartbeat({
        "task": {
            "id": "hb-1",
            "prompt": "Deploy to staging",
            "created_by": "user-1",
        }
    })

    assert result["status"] == "ok"
    handler.assert_called_once()
    msg = handler.call_args[0][0]
    assert msg.platform == "paperclip"
    assert "Deploy to staging" in msg.content
    assert msg.metadata.get("heartbeat") is True


@pytest.mark.asyncio
async def test_heartbeat_with_agent_mapping(pc):
    handler = AsyncMock()
    pc.set_message_handler(handler)

    result = await pc.handle_heartbeat({
        "task": {
            "id": "hb-2",
            "prompt": "Fix the auth bug",
            "agent": "albert",
        }
    })

    assert result["status"] == "ok"
    msg = handler.call_args[0][0]
    assert msg.content.startswith("@albert")


@pytest.mark.asyncio
async def test_heartbeat_no_prompt_returns_error(pc):
    handler = AsyncMock()
    pc.set_message_handler(handler)

    result = await pc.handle_heartbeat({"task": {"id": "hb-3"}})

    assert result["status"] == "error"
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_no_handler_returns_error(pc):
    result = await pc.handle_heartbeat({
        "task": {"id": "hb-4", "prompt": "Do something"}
    })

    assert result["status"] == "error"
    assert "No message handler" in result["error"]


# ------------------------------------------------------------------ #
# Task polling with agent mapping
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_check_tasks_routes_to_agent(pc):
    handler = AsyncMock()
    pc.set_message_handler(handler)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"id": "t1", "prompt": "Review the PR", "agent": "max"},
    ]

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    pc._client = mock_client

    await pc._check_tasks()

    handler.assert_called_once()
    msg = handler.call_args[0][0]
    assert msg.content.startswith("@max")
    assert "Review the PR" in msg.content


@pytest.mark.asyncio
async def test_check_tasks_uses_configured_limit(pc):
    handler = AsyncMock()
    pc.set_message_handler(handler)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = []

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    pc._client = mock_client

    await pc._check_tasks()

    call_args = mock_client.get.call_args
    assert call_args.kwargs["params"]["limit"] == 3  # from fixture


@pytest.mark.asyncio
async def test_check_tasks_handles_http_error(pc):
    handler = AsyncMock()
    pc.set_message_handler(handler)

    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "Service Unavailable"

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    pc._client = mock_client

    # Should not raise
    await pc._check_tasks()
    handler.assert_not_called()


# ------------------------------------------------------------------ #
# Config integration
# ------------------------------------------------------------------ #


def test_config_loads_paperclip_fields():
    from claude_daemon.core.config import DaemonConfig

    config = DaemonConfig()
    assert config.paperclip_task_limit == 5
    assert config.paperclip_startup_timeout == 30
    assert config.paperclip_poll_interval == 5
    assert config.paperclip_url is None
    assert config.paperclip_api_key is None
