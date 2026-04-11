"""Tests for the MessageRouter including rate limiting."""

from __future__ import annotations

import pytest

from claude_daemon.integrations.router import MessageRouter, RateLimiter


class FakeDaemon:
    class FakeConfig:
        rate_limit_per_user = 3
        rate_limit_window = 60

    config = FakeConfig()

    async def handle_message(self, prompt, platform="cli", user_id="local"):
        return f"Response to: {prompt[:50]}"


def test_split_message_short():
    router = MessageRouter(FakeDaemon())
    chunks = router._split_message("Hello world", "telegram")
    assert chunks == ["Hello world"]


def test_split_message_long():
    router = MessageRouter(FakeDaemon())
    long_msg = "A" * 5000
    chunks = router._split_message(long_msg, "telegram")
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)


def test_split_message_discord_limit():
    router = MessageRouter(FakeDaemon())
    msg = "B" * 3000
    chunks = router._split_message(msg, "discord")
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)


def test_rate_limiter_allows():
    rl = RateLimiter(max_requests=3, window_seconds=60)
    assert rl.is_allowed("user1") is True
    assert rl.is_allowed("user1") is True
    assert rl.is_allowed("user1") is True


def test_rate_limiter_blocks():
    rl = RateLimiter(max_requests=2, window_seconds=60)
    assert rl.is_allowed("user1") is True
    assert rl.is_allowed("user1") is True
    assert rl.is_allowed("user1") is False  # Blocked


def test_rate_limiter_per_user():
    rl = RateLimiter(max_requests=1, window_seconds=60)
    assert rl.is_allowed("user1") is True
    assert rl.is_allowed("user2") is True  # Different user
    assert rl.is_allowed("user1") is False  # Same user blocked
