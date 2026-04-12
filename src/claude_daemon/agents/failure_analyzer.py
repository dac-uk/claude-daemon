"""FailureAnalyzer — automated post-mortem and lesson extraction.

When agent tasks fail, this module:
1. Classifies the failure (rate_limit, timeout, tool_error, logic_error, etc.)
2. Extracts a one-sentence lesson
3. Records to failure_analyses table for pattern detection
4. Writes lessons to shared/failure-lessons.md for cross-agent learning
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.core.process import ProcessManager
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)

FAILURE_ANALYSIS_PROMPT = """\
Analyze this task failure concisely.

Agent: {agent_name}
Task type: {task_type}
Error output:
{error_text}

Respond with ONLY valid JSON (no markdown, no explanation):
{{
    "category": "<rate_limit|timeout|tool_error|logic_error|context_overflow|permission_denied|unknown>",
    "root_cause": "<one sentence>",
    "lesson": "<one actionable sentence for preventing this in future>",
    "severity": "<low|medium|high>",
    "recurrence_risk": "<low|medium|high>"
}}
"""


class FailureAnalyzer:
    """Analyzes task failures and extracts reusable lessons."""

    def __init__(
        self,
        process_manager: ProcessManager,
        store: ConversationStore,
        shared_dir: Path,
    ) -> None:
        self.pm = process_manager
        self.store = store
        self.shared_dir = shared_dir
        self._lessons_file = shared_dir / "failure-lessons.md"

    async def analyze(
        self, agent_name: str, task_type: str, error_text: str,
    ) -> dict | None:
        """Classify a failure using a cheap Haiku call. Returns analysis dict or None."""
        # Deduplicate: skip if we've seen this exact error recently
        error_hash = hashlib.sha256(error_text[:500].encode()).hexdigest()[:16]
        recent = self.store.get_recent_failures(agent_name, limit=5)
        if any(f.get("error_hash") == error_hash for f in recent):
            log.debug("Skipping duplicate failure analysis for %s (hash=%s)", agent_name, error_hash)
            return None

        prompt = FAILURE_ANALYSIS_PROMPT.format(
            agent_name=agent_name,
            task_type=task_type,
            error_text=error_text[:1500],
        )

        try:
            response = await self.pm.send_message(
                prompt=prompt,
                system_context="You are a failure analysis system. Respond with JSON only.",
                model="haiku",
                max_tokens=300,
            )
            analysis = json.loads(response.result.strip())
        except (json.JSONDecodeError, Exception) as e:
            log.debug("Failed to analyze failure for %s: %s", agent_name, e)
            return None

        # Ensure all expected keys exist
        for key in ("category", "root_cause", "lesson", "severity", "recurrence_risk"):
            if key not in analysis:
                analysis[key] = "unknown"

        # Persist to DB
        try:
            self.store.record_failure(
                agent_name=agent_name,
                task_type=task_type,
                category=analysis["category"],
                root_cause=analysis["root_cause"],
                lesson=analysis["lesson"],
                severity=analysis["severity"],
                recurrence_risk=analysis["recurrence_risk"],
                error_hash=error_hash,
            )
        except Exception:
            log.debug("Could not persist failure analysis")

        # Write lesson to shared file
        self._write_lesson(agent_name, analysis)

        log.info(
            "Failure analyzed for %s: category=%s, severity=%s",
            agent_name, analysis["category"], analysis["severity"],
        )
        return analysis

    def _write_lesson(self, agent_name: str, analysis: dict) -> None:
        """Append lesson to shared/failure-lessons.md."""
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        if not self._lessons_file.exists():
            self._lessons_file.write_text("# Failure Lessons\n\nAuto-generated from failure analysis.\n\n")

        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        entry = (
            f"- [{ts}] **{agent_name}** ({analysis['category']}, {analysis['severity']}): "
            f"{analysis['lesson']}\n"
        )
        with open(self._lessons_file, "a") as f:
            f.write(entry)

    def get_patterns(self, days: int = 7) -> list[dict]:
        """Get recurring failure patterns from the DB."""
        return self.store.get_failure_patterns(days)

    def get_lessons_summary(self, max_chars: int = 800) -> str:
        """Read failure lessons for injection into improvement planner context."""
        if self._lessons_file.exists():
            content = self._lessons_file.read_text()
            if len(content) > max_chars:
                content = content[-max_chars:]
            return content
        return ""
