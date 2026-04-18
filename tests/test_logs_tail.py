"""Tests for utils.logs.tail_log — log file parsing for the Alerts view."""

from __future__ import annotations

from pathlib import Path

from claude_daemon.utils.logs import tail_log


def _sample(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def test_missing_file_returns_empty(tmp_path: Path):
    assert tail_log(tmp_path / "nope.log") == []


def test_parses_standard_format(tmp_path: Path):
    log = tmp_path / "daemon.log"
    log.write_text(_sample([
        "2026-04-18 12:00:00 [INFO] claude_daemon.core: booting",
        "2026-04-18 12:00:01 [WARNING] claude_daemon.agents: mcp unhealthy",
        "2026-04-18 12:00:02 [ERROR] claude_daemon.core: oh no",
    ]))
    out = tail_log(log, min_level="WARNING")
    assert [e["level"] for e in out] == ["WARNING", "ERROR"]
    assert out[0]["logger"] == "claude_daemon.agents"
    assert out[1]["message"] == "oh no"


def test_level_filter_drops_lower_levels(tmp_path: Path):
    log = tmp_path / "daemon.log"
    log.write_text(_sample([
        "2026-04-18 12:00:00 [INFO] x: info line",
        "2026-04-18 12:00:01 [DEBUG] x: debug line",
        "2026-04-18 12:00:02 [WARNING] x: warn line",
    ]))
    out = tail_log(log, min_level="ERROR")
    assert out == []


def test_traceback_attached_to_previous_entry(tmp_path: Path):
    log = tmp_path / "daemon.log"
    log.write_text(_sample([
        "2026-04-18 12:00:00 [ERROR] claude_daemon.core: boom",
        "Traceback (most recent call last):",
        '  File "foo.py", line 1, in <module>',
        "    raise ValueError('x')",
        "ValueError: x",
        "2026-04-18 12:00:05 [INFO] claude_daemon.core: recovered",
    ]))
    out = tail_log(log, min_level="WARNING")
    assert len(out) == 1
    assert out[0]["level"] == "ERROR"
    assert "Traceback" in out[0]["traceback"]
    assert "ValueError: x" in out[0]["traceback"]


def test_since_filter_drops_old_entries(tmp_path: Path):
    log = tmp_path / "daemon.log"
    log.write_text(_sample([
        "2026-04-18 11:00:00 [WARNING] x: old",
        "2026-04-18 12:00:00 [WARNING] x: new",
    ]))
    out = tail_log(log, since="2026-04-18 11:30:00")
    assert [e["message"] for e in out] == ["new"]


def test_lines_limit_caps_tail(tmp_path: Path):
    log = tmp_path / "daemon.log"
    raw = []
    for i in range(20):
        raw.append(f"2026-04-18 12:00:{i:02d} [WARNING] x: line {i}")
    log.write_text(_sample(raw))
    out = tail_log(log, lines=5, min_level="WARNING")
    assert len(out) == 5
    assert out[-1]["message"] == "line 19"


def test_partial_first_line_is_dropped(tmp_path: Path):
    # Simulate the bounded-read landing mid-line by writing garbage before a
    # valid entry.
    log = tmp_path / "daemon.log"
    log.write_text(
        "garbage-partial-line-from-prior-rotation\n"
        "2026-04-18 12:00:00 [WARNING] claude_daemon.core: valid\n"
    )
    out = tail_log(log, min_level="WARNING")
    assert len(out) == 1
    assert out[0]["message"] == "valid"
