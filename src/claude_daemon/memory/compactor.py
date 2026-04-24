"""Three-phase dreaming system inspired by OpenClaw.

Dreaming is a memory consolidation pipeline with three distinct phases:

1. LIGHT SLEEP (hourly) - Signal detection
   Scans recent activity for noteworthy signals (decisions, preferences, facts).
   Cheap, fast, runs frequently. Tags signals with importance scores.

2. DEEP SLEEP (nightly) - Consolidation
   Summarizes active sessions, merges related signals, deduplicates.
   Writes daily log summaries. Archives expired sessions.

3. REM SLEEP (weekly) - Integration
   Reads accumulated signals + existing MEMORY.md + SOUL.md learnings.
   Extracts cross-session patterns. Updates persistent memory.
   Generates self-reflections for the improvement loop.

Each phase produces explainable output: what was promoted, why, and what was discarded.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.core.process import ProcessManager
    from claude_daemon.memory.durable import DurableMemory
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)

# -- Phase 1: Light Sleep --

LIGHT_SLEEP_PROMPT = """\
You are scanning recent conversation activity for noteworthy signals.
Extract ONLY genuinely important items from this conversation. For each signal, provide:
- The signal (fact, preference, decision, or pattern)
- Why it matters (one sentence)
- Importance: HIGH, MEDIUM, or LOW

Be selective. Most conversations contain 0-2 signals worth remembering.
Output format - one signal per line:
[HIGH] Signal text | Why it matters
[MEDIUM] Signal text | Why it matters

If nothing noteworthy, respond with exactly: NO_SIGNALS

Conversation:
{conversation}
"""

# -- Phase 2: Deep Sleep --

DEEP_SLEEP_PROMPT = """\
You are consolidating today's memory signals. Below are signals detected from conversations today,
plus the current persistent memory. Your tasks:

1. Remove duplicate or redundant signals
2. Merge related signals into coherent insights
3. Summarize the day's key activity
4. Note any contradictions with existing memory

Output a concise daily summary (under 800 chars) with sections:
## Key Insights
## Patterns Noticed
## Contradictions (if any)

Today's signals:
{signals}

Current persistent memory:
{memory}
"""

# -- Phase 3: REM Sleep --

REM_SLEEP_PROMPT = """\
You are performing deep memory integration. You have access to:
1. Your persistent memory (accumulated knowledge about the user)
2. Recent daily summaries (last 7 days of consolidated insights)
3. Your soul/identity document (who you are)
4. Your self-reflections (what you've learned about being effective)

Your task is to produce an updated persistent memory document that:
- Integrates new patterns discovered this week
- Removes information that's no longer relevant or accurate
- Resolves contradictions (prefer recent evidence)
- Organizes knowledge into clear categories
- Preserves the user's core preferences and facts
- Stays under {max_chars} characters

For each major change, add a brief annotation: <!-- promoted from [date] --> or <!-- removed: outdated -->

Current persistent memory:
{memory}

Recent daily summaries:
{summaries}

Soul/Identity:
{soul}

Self-reflections:
{reflections}

Write the updated persistent memory document:
"""

# -- Self-reflection --

REFLECTION_PROMPT = """\
Review these recent interactions and reflect on your effectiveness as an assistant.
Consider:
- What responses worked well? What made them effective?
- Where did you fall short? What could you do differently?
- What patterns in the user's requests suggest about their needs?
- Are there tools, approaches, or knowledge areas you should prioritize?

Keep reflections actionable and specific (under 600 chars).
Format: bullet points starting with what you learned.

Recent interactions:
{interactions}

Previous reflections:
{reflections}
"""


class ContextCompactor:
    """Three-phase dreaming system for memory consolidation."""

    def __init__(
        self,
        store: ConversationStore,
        durable: DurableMemory,
        process_manager: ProcessManager,
        config: DaemonConfig | None = None,
        embedding_store=None,
    ) -> None:
        self.store = store
        self.durable = durable
        self.pm = process_manager
        self.config = config
        self._embedding_store = embedding_store

    # ------------------------------------------------------------------ #
    # Phase 1: LIGHT SLEEP - Signal detection (runs frequently)
    # ------------------------------------------------------------------ #

    async def light_sleep(self, session_id: str) -> list[str]:
        """Scan a single conversation for noteworthy signals.

        Called after each message exchange or periodically.
        Returns list of detected signals.
        """
        conv = self.store.get_conversation_by_session(session_id)
        if not conv:
            return []

        text = self.store.get_conversation_text(conv["id"], limit=15)
        if not text or len(text) < 200:
            return []

        prompt = LIGHT_SLEEP_PROMPT.format(conversation=text)
        response = await self.pm.send_message(
            prompt=prompt, max_budget=0.05, platform="system", user_id="dreamer",
        )

        if response.is_error or "NO_SIGNALS" in response.result:
            return []

        # Parse signals
        signals = []
        for line in response.result.strip().split("\n"):
            line = line.strip()
            if line.startswith("[HIGH]") or line.startswith("[MEDIUM]") or line.startswith("[LOW]"):
                signals.append(line)

        if signals:
            # Store signals as a summary for later consolidation
            self.store.add_summary(conv["id"], "\n".join(signals), "light_sleep")
            self.durable.append_daily_log(
                f"Light sleep: {len(signals)} signals from session {session_id[:8]}"
            )
            log.info("Light sleep: %d signals from session %s", len(signals), session_id[:8])

        return signals

    # ------------------------------------------------------------------ #
    # Phase 2: DEEP SLEEP - Consolidation (runs nightly)
    # ------------------------------------------------------------------ #

    async def deep_sleep(self) -> None:
        """Nightly consolidation: merge signals, summarize sessions, clean up.

        This replaces the old daily_compaction method.
        """
        log.info("Deep sleep starting...")

        # Gather all light_sleep signals from today
        today_signals = self.store.get_summaries_by_type("light_sleep", limit=50)
        all_signals = "\n".join(today_signals) if today_signals else "(no signals detected today)"

        memory = self.durable.read_memory()

        # Ask Claude to consolidate
        prompt = DEEP_SLEEP_PROMPT.format(
            signals=all_signals,
            memory=memory or "(empty)",
        )
        response = await self.pm.send_message(
            prompt=prompt, max_budget=0.10, platform="system", user_id="dreamer",
        )

        # Retry once on 0-token response (transient API issue)
        if not response.is_error and response.output_tokens == 0:
            log.warning("Deep sleep: 0 output tokens (cost=$%.4f), retrying", response.cost)
            response = await self.pm.send_message(
                prompt=prompt, max_budget=0.10, platform="system", user_id="dreamer",
            )

        if not response.is_error and len(response.result) > 30:
            # Store daily summary
            self.store.add_summary(None, response.result, "deep_sleep")
            self.durable.append_daily_log(f"Deep sleep summary:\n{response.result[:500]}")
            log.info("Deep sleep: daily summary written (%d chars)", len(response.result))
        elif response.is_error:
            log.warning("Deep sleep: API error: %s", response.result[:200])
        else:
            log.warning(
                "Deep sleep: consolidation too short (%d chars, %d output tokens)",
                len(response.result), response.output_tokens,
            )

        # Archive expired sessions
        archived = self.store.cleanup_expired(
            self.config.max_session_age_hours if self.config else 72
        )
        if archived:
            self.durable.append_daily_log(f"Archived {archived} expired conversations.")

        # Garbage collect old daily logs
        if self.config:
            self.durable.cleanup_old_logs(self.config.log_retention_days)

        # Reindex semantic memory embeddings
        if self._embedding_store and self._embedding_store.available:
            try:
                agents_dir = self.config.data_dir / "agents" if self.config else None
                shared_dir = self.config.data_dir / "shared" if self.config else None
                if agents_dir and shared_dir:
                    count = await self._embedding_store.reindex_all(agents_dir, shared_dir)
                    if count:
                        log.info("Deep sleep: reindexed %d memory chunks", count)
            except Exception:
                log.debug("Embedding reindex failed during deep sleep")

            try:
                conv_count = await self._embedding_store.reindex_conversations(days=7)
                if conv_count:
                    log.info("Deep sleep: reindexed %d conversation chunks", conv_count)
            except Exception:
                log.debug("Conversation reindex failed during deep sleep")

        log.info("Deep sleep complete")

    # ------------------------------------------------------------------ #
    # Phase 3: REM SLEEP - Integration (runs weekly)
    # ------------------------------------------------------------------ #

    async def rem_sleep(self) -> None:
        """Weekly deep integration: update MEMORY.md with accumulated insights.

        This replaces the old auto_dream method.
        """
        log.info("REM sleep starting...")

        memory = self.durable.read_memory()
        soul = self.durable.read_soul()
        reflections = self.durable.read_reflections()

        # Gather deep_sleep summaries from the last 7 days
        summaries = self.store.get_summaries_by_type("deep_sleep", limit=7)
        summaries_text = "\n---\n".join(summaries) if summaries else "(no daily summaries)"

        max_chars = self.config.max_memory_chars if self.config else 3000

        prompt = REM_SLEEP_PROMPT.format(
            memory=memory or "(empty)",
            summaries=summaries_text,
            soul=soul or "(no soul document)",
            reflections=reflections or "(no reflections yet)",
            max_chars=max_chars,
        )
        response = await self.pm.send_message(
            prompt=prompt, max_budget=0.15, platform="system", user_id="dreamer",
            task_type="rem_sleep",
        )

        if response.is_error:
            log.error("REM sleep failed: %s", response.result[:200])
            self.durable.append_daily_log("REM sleep: FAILED - " + response.result[:100])
            return

        new_memory = response.result
        if len(new_memory) < 50:
            log.warning("REM sleep produced suspiciously short output, skipping")
            return

        # Version the old memory before overwriting
        self.durable.archive_memory()

        # Write new memory (with validation to prevent catastrophic data loss)
        if not self.durable.update_memory(new_memory, validate=True):
            self.durable.append_daily_log(
                "REM sleep: MEMORY.md update REJECTED by validation. Archive preserved."
            )
            return

        self.store.add_summary(None, new_memory, "rem_sleep")

        # Run self-reflection if enabled
        if self.config and self.config.self_improve:
            await self._reflect()

        log.info("REM sleep complete: MEMORY.md updated (%d chars)", len(new_memory))

    # ------------------------------------------------------------------ #
    # Self-improvement: Reflexion
    # ------------------------------------------------------------------ #

    async def _reflect(self) -> None:
        """Generate self-reflections on recent performance.

        Produces actionable insights stored in REFLECTIONS.md.
        """
        log.info("Generating self-reflections...")

        # Get recent conversation excerpts
        convos = self.store.get_active_conversations()
        interaction_texts = []
        for conv in convos[:5]:
            text = self.store.get_conversation_text(conv["id"], limit=10)
            if text:
                interaction_texts.append(text[:500])

        if not interaction_texts:
            return

        reflections = self.durable.read_reflections()

        prompt = REFLECTION_PROMPT.format(
            interactions="\n---\n".join(interaction_texts),
            reflections=reflections or "(first reflection)",
        )
        response = await self.pm.send_message(
            prompt=prompt, max_budget=0.08, platform="system", user_id="reflector",
        )

        if not response.is_error and len(response.result) > 30:
            self.durable.update_reflections(response.result)
            self.durable.append_daily_log("Self-reflection updated.")
            log.info("Self-reflection complete")

    # ------------------------------------------------------------------ #
    # Legacy API compatibility
    # ------------------------------------------------------------------ #

    async def compact_session(self, session_id: str) -> str | None:
        """Legacy: runs light_sleep on a single session."""
        signals = await self.light_sleep(session_id)
        return "\n".join(signals) if signals else None

    async def daily_compaction(self) -> None:
        """Legacy: runs deep_sleep."""
        await self.deep_sleep()

    async def auto_dream(self) -> None:
        """Legacy: runs rem_sleep."""
        await self.rem_sleep()

    # ------------------------------------------------------------------ #
    # Per-agent memory compaction
    # ------------------------------------------------------------------ #

    async def compact_agent_memory(self, agent) -> None:
        """Run memory compaction for a specific agent's MEMORY.md.

        Reads the agent's recent conversations and consolidates them
        into the agent's own MEMORY.md file.
        """
        from claude_daemon.agents.agent import Agent

        memory_path = agent.workspace / "MEMORY.md"
        current_memory = memory_path.read_text() if memory_path.exists() else ""

        # Get recent conversations for this agent
        convos = self.store.get_active_conversations()
        agent_convos = [
            c for c in convos
            if c.get("user_id", "").endswith(f":{agent.name}")
        ]

        if not agent_convos:
            return

        interaction_texts = []
        for conv in agent_convos[:10]:
            text = self.store.get_conversation_text(conv["id"], limit=15)
            if text:
                interaction_texts.append(text[:800])

        if not interaction_texts:
            return

        prompt = (
            f"You are helping agent '{agent.name}' ({agent.identity.role}) "
            f"consolidate their persistent memory.\n\n"
            f"Current memory:\n{current_memory[:2000]}\n\n"
            f"Recent conversations:\n{'---'.join(interaction_texts)}\n\n"
            f"Produce the updated memory document. Preserve important facts, "
            f"preferences, and decisions. Add new learnings from the conversations. "
            f"Remove outdated info. Keep it concise and actionable.\n\n"
            f"CRITICAL OUTPUT RULES:\n"
            f"- Respond with the memory content ONLY — plain text/markdown.\n"
            f"- Do NOT call any tools.\n"
            f"- Do NOT add preamble, acknowledgement, or trailing commentary.\n"
            f"- Do NOT wrap the output in code fences.\n"
            f"- The first character of your response must be the first character "
            f"of the new memory document."
        )

        response = await self.pm.send_message(
            prompt=prompt, max_budget=0.10,
            platform="system", user_id=f"compactor:{agent.name}",
            model_override=agent.get_model("scheduled"),
            disable_tools=True,
        )

        # Retry once on 0-token response (transient API issue)
        if not response.is_error and response.output_tokens == 0:
            log.warning(
                "Agent memory compaction for %s: 0 output tokens (cost=$%.4f), retrying",
                agent.name, response.cost,
            )
            response = await self.pm.send_message(
                prompt=prompt, max_budget=0.10,
                platform="system", user_id=f"compactor:{agent.name}",
                model_override=agent.get_model("scheduled"),
                disable_tools=True,
            )

        if response.is_error or len(response.result) < 30:
            preview = (response.result or "").strip().replace("\n", " ")[:120]
            log.warning(
                "Agent memory compaction for %s produced bad output "
                "(is_error=%s, len=%d, tokens=%d, turns=%d, preview=%r)",
                agent.name, response.is_error, len(response.result),
                response.output_tokens, response.num_turns, preview,
            )
            return

        new_memory = response.result

        # Validate — don't clobber with much smaller content
        if current_memory and len(new_memory) < len(current_memory) * 0.3:
            log.warning(
                "Agent %s memory update rejected: %d -> %d chars (too much loss)",
                agent.name, len(current_memory), len(new_memory),
            )
            return

        memory_path.write_text(new_memory)
        log.info("Agent %s MEMORY.md updated (%d chars)", agent.name, len(new_memory))

    async def compact_all_agent_memories(self, registry) -> None:
        """Run memory compaction for all agents in the registry."""
        for agent in registry:
            try:
                await self.compact_agent_memory(agent)
            except Exception:
                log.exception("Failed to compact memory for agent %s", agent.name)
