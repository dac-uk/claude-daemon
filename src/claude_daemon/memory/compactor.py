"""ContextCompactor - summarization, daily compaction, and auto-dream.

Uses Claude itself (via ProcessManager) to summarize conversations
and consolidate memory.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.core.process import ProcessManager
    from claude_daemon.memory.durable import DurableMemory
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)

SUMMARIZE_PROMPT = """\
Summarize the following conversation concisely. Focus on:
- Key decisions made
- Important facts learned about the user
- Tasks completed or in progress
- User preferences discovered

Keep the summary under 500 characters.

Conversation:
{conversation}
"""

DREAM_PROMPT = """\
You are consolidating your memory. Below are your recent daily activity logs
and current persistent memory. Your task:

1. Extract recurring patterns, preferences, and important facts
2. Remove outdated or redundant information
3. Write a clean, updated persistent memory document

Keep it concise (under 1500 characters). Use markdown headers to organize.

Current persistent memory:
{memory}

Recent activity logs (last 7 days):
{logs}

Write the updated persistent memory document:
"""


class ContextCompactor:
    """Manages memory summarization and consolidation."""

    def __init__(
        self,
        store: ConversationStore,
        durable: DurableMemory,
        process_manager: ProcessManager,
    ) -> None:
        self.store = store
        self.durable = durable
        self.pm = process_manager

    async def compact_session(self, session_id: str) -> str | None:
        """Summarize a conversation and store the summary."""
        # Find conversation
        row = self.store._db.execute(
            "SELECT id FROM conversations WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None

        conv_id = row["id"]
        text = self.store.get_conversation_text(conv_id, limit=30)
        if not text or len(text) < 100:
            return None

        prompt = SUMMARIZE_PROMPT.format(conversation=text)
        response = await self.pm.send_message(
            prompt=prompt,
            max_budget=0.10,
            platform="system",
            user_id="compactor",
        )

        if response.is_error:
            log.error("Compaction failed: %s", response.result)
            return None

        summary = response.result
        self.store.add_summary(conv_id, summary, "session")
        log.info("Compacted session %s: %d chars", session_id, len(summary))
        return summary

    async def daily_compaction(self) -> None:
        """Scheduled job: summarize all active sessions and write daily log."""
        log.info("Running daily compaction...")

        conversations = self.store.get_active_conversations()
        compacted = 0

        for conv in conversations:
            if conv["message_count"] > 5:  # Only compact conversations with activity
                result = await self.compact_session(conv["session_id"])
                if result:
                    compacted += 1

        # Write summary to daily log
        self.durable.append_daily_log(
            f"Daily compaction: {compacted}/{len(conversations)} conversations summarized."
        )

        # Clean up old sessions
        archived = self.store.cleanup_expired()
        if archived:
            self.durable.append_daily_log(f"Archived {archived} expired conversations.")

        log.info("Daily compaction complete: %d compacted, %d archived", compacted, archived)

    async def auto_dream(self) -> None:
        """KAIROS-inspired: consolidate weekly memories into MEMORY.md.

        Reads recent daily logs and current MEMORY.md, asks Claude to
        extract patterns and preferences, then updates MEMORY.md.
        """
        log.info("Running auto-dream memory consolidation...")

        memory = self.durable.read_memory()
        logs = self.durable.read_recent_logs(days=7)

        if not logs and not memory:
            log.info("No memory content to consolidate")
            return

        prompt = DREAM_PROMPT.format(memory=memory or "(empty)", logs=logs or "(no recent logs)")
        response = await self.pm.send_message(
            prompt=prompt,
            max_budget=0.15,
            platform="system",
            user_id="dreamer",
        )

        if response.is_error:
            log.error("Auto-dream failed: %s", response.result)
            return

        # Update MEMORY.md with consolidated memory
        new_memory = response.result
        if len(new_memory) > 50:  # Sanity check
            self.durable.update_memory(new_memory)
            self.store.add_summary(None, new_memory, "dream")
            self.durable.append_daily_log("Auto-dream: persistent memory consolidated.")
            log.info("Auto-dream complete: MEMORY.md updated (%d chars)", len(new_memory))
        else:
            log.warning("Auto-dream produced suspiciously short output, skipping update")
