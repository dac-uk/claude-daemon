"""Tests for scheduler helpers."""

from __future__ import annotations

import pytest

from claude_daemon.scheduler.engine import _parse_cron


def test_parse_cron_standard():
    """Test parsing a standard cron expression."""
    result = _parse_cron("0 3 * * *")
    assert result == {
        "minute": "0",
        "hour": "3",
        "day": "*",
        "month": "*",
        "day_of_week": "*",
    }


def test_parse_cron_complex():
    """Test parsing a complex cron expression."""
    result = _parse_cron("30 9 * * 1-5")
    assert result["minute"] == "30"
    assert result["hour"] == "9"
    assert result["day_of_week"] == "1-5"


def test_parse_cron_invalid():
    """Test that invalid cron expressions raise ValueError."""
    with pytest.raises(ValueError, match="Invalid cron expression"):
        _parse_cron("invalid")

    with pytest.raises(ValueError, match="Invalid cron expression"):
        _parse_cron("* * *")
