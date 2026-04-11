"""WorkingMemory - assembles context for injection into Claude prompts.

Builds the --append-system-prompt content from:
1. MEMORY.md (persistent facts and preferences)
2. Recent daily logs (last 3 days)
3. Conversation summary (if available)
4. Session metadata
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.memory.durable import DurableMemory
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 5000


class WorkingMemory:
    """Assembles context for Claude prompt injection."""

    def __init__(self, store: ConversationStore, durable: DurableMemory) -> None:
        self.store = store
        self.durable = durable

    def build_context(self, session_id: str) -> str:
        """Build context string for a given session.

        Returns a string suitable for --append-system-prompt.
        """
        blocks = []

        # 1. Persistent memory (MEMORY.md)
        memory = self.durable.read_memory()
        if memory:
            blocks.append(f"## Your Persistent Memory\n{memory}")

        # 2. Recent daily logs
        recent = self.durable.read_recent_logs(days=3)
        if recent:
            if len(recent) > 1500:
                recent = recent[-1500:]
            blocks.append(f"## Recent Activity (last 3 days)\n{recent}")

        # 3. Conversation summary (if this session has been compacted)
        try:
            # Find conversation by session_id
            rows = self.store._db.execute(
                "SELECT c.id FROM conversations c WHERE c.session_id = ?",
                (session_id,),
            ).fetchone()
            if rows:
                summary = self.store.get_latest_summary(rows["id"])
                if summary:
                    blocks.append(f"## Conversation Summary\n{summary}")
        except Exception:
            pass  # Non-critical

        context = "\n\n".join(blocks)

        # Enforce size limit
        if len(context) > MAX_CONTEXT_CHARS:
            context = context[-MAX_CONTEXT_CHARS:]
            log.debug("Context truncated to %d chars", MAX_CONTEXT_CHARS)

        if context:
            header = (
                "You are running as Claude Daemon, a persistent background assistant. "
                "Below is your accumulated context from previous sessions and recent activity. "
                "Use this to maintain continuity.\n\n"
            )
            context = header + context

        return context

    def estimate_tokens(self, text: str) -> int:
        """Rough token estimation (chars / 4)."""
        return len(text) // 4
