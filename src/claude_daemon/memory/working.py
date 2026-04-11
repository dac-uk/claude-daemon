"""WorkingMemory - assembles context for injection into Claude prompts.

Builds the --append-system-prompt content from:
1. SOUL.md (identity and personality)
2. MEMORY.md (persistent knowledge)
3. REFLECTIONS.md (self-improvement insights)
4. Recent daily logs (last 3 days)
5. Conversation summary (if available)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.memory.durable import DurableMemory
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)


class WorkingMemory:
    """Assembles context for Claude prompt injection."""

    def __init__(
        self,
        store: ConversationStore,
        durable: DurableMemory,
        config: DaemonConfig | None = None,
    ) -> None:
        self.store = store
        self.durable = durable
        self.config = config

    def build_context(self, session_id: str) -> str:
        """Build context string for a given session."""
        max_chars = self.config.max_context_chars if self.config else 5000

        # Use the durable memory's comprehensive context builder
        context = self.durable.get_context_block(recent_days=3, max_chars=max_chars - 500)

        # Add conversation-specific summary if available
        conv = self.store.get_conversation_by_session(session_id)
        if conv:
            summary = self.store.get_latest_summary(conv["id"])
            if summary:
                context += f"\n\n## Conversation Context\n{summary[:500]}"

        # Enforce size limit
        if len(context) > max_chars:
            context = context[:max_chars]

        if context:
            header = (
                "You are running as Claude Daemon, a persistent background assistant. "
                "You maintain continuity across conversations and platforms. "
                "Below is your accumulated context.\n\n"
            )
            context = header + context

        return context
