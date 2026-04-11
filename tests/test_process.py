"""Tests for the ProcessManager."""

from __future__ import annotations

import json

import pytest

from claude_daemon.core.process import ClaudeResponse


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
