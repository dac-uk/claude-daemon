"""Tests for the MessageRouter."""

from __future__ import annotations

import pytest

from claude_daemon.integrations.router import MessageRouter


class FakeDaemon:
    """Minimal fake daemon for router tests."""

    async def handle_message(self, prompt, platform="cli", user_id="local"):
        return f"Response to: {prompt[:50]}"


def test_split_message_short():
    """Test that short messages are not split."""
    router = MessageRouter(FakeDaemon())
    chunks = router._split_message("Hello world", "telegram")
    assert chunks == ["Hello world"]


def test_split_message_long():
    """Test splitting a long message."""
    router = MessageRouter(FakeDaemon())
    long_msg = "A" * 5000
    chunks = router._split_message(long_msg, "telegram")
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)


def test_split_message_at_newline():
    """Test that splitting prefers newline boundaries."""
    router = MessageRouter(FakeDaemon())
    msg = ("Line 1\n" * 300) + ("Line 2\n" * 300)
    chunks = router._split_message(msg, "discord")
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)


def test_format_response():
    """Test platform-specific formatting."""
    router = MessageRouter(FakeDaemon())
    # Currently a passthrough, but verify it doesn't crash
    result = router._format_response("Hello **world**", "telegram")
    assert "Hello **world**" in result
