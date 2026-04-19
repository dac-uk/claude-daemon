"""Regression tests for stop_reason propagation and infra-failure refund.

Context: an Opus turn completed with `cost=$0.5118, is_error=True,
result=''` because the SDK bridge idle-timed out mid-stream after cost
had already accumulated. Two guards were added:

- `stop_reason` now flows from bridge.js → sdk_bridge.py → ClaudeResponse
  so the orchestrator's "no usable response" warning can distinguish a
  clean end_turn (empty tool-only turn) from a refusal/max_tokens.
- The orchestrator refunds recorded cost to $0 when `is_error` AND the
  result text contains one of the SDK-bridge infra-failure markers.
"""

from __future__ import annotations

from claude_daemon.core.process import ClaudeResponse


def test_claude_response_has_stop_reason_field():
    """Default must be empty string so downstream truthy/equality checks
    (`stop_reason == "end_turn"`) don't raise AttributeError."""
    r = ClaudeResponse.error("boom")
    assert hasattr(r, "stop_reason")
    assert r.stop_reason == ""


def test_claude_response_from_json_parses_stop_reason():
    r = ClaudeResponse.from_json({
        "result": "hello",
        "session_id": "abc",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "num_turns": 1,
        "duration_ms": 100,
        "is_error": False,
        "stop_reason": "end_turn",
    })
    assert r.stop_reason == "end_turn"
    assert r.result == "hello"


def test_claude_response_from_json_defaults_stop_reason_empty():
    r = ClaudeResponse.from_json({
        "result": "hello", "session_id": "abc", "total_cost_usd": 0,
        "usage": {}, "num_turns": 1, "duration_ms": 100, "is_error": False,
    })
    assert r.stop_reason == ""


def test_infra_failure_markers_trigger_refund():
    """The orchestrator refunds cost iff is_error AND result contains
    one of these markers. Keep this test in sync with orchestrator.py."""
    markers = (
        "idle timeout",
        "stream ended without result",
        "bridge process may have crashed",
    )
    for marker in markers:
        msg = f"SDK bridge: {marker} — blah blah"
        assert any(m in msg for m in markers), marker

    # Legitimate user-facing errors must NOT match any marker.
    for benign in ("rate limit exceeded", "prompt too long", "model refused"):
        assert not any(m in benign for m in markers), benign
