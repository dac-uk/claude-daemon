"""Orchestrator - routes messages to named agents and manages delegation.

Supports parallel task dispatch: multiple tasks to the same agent run
concurrently in separate sessions (no --resume collision).

The orchestrator is itself an agent, but with special routing responsibilities.
When a message arrives, it either:
1. Routes to a specifically addressed agent (@agent_name or /agent_name)
2. Lets the orchestrator agent decide who should handle it
3. Falls through to the orchestrator for direct handling
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, AsyncIterator

from claude_daemon.agents.agent import Agent
from claude_daemon.core.process import ClaudeResponse

if TYPE_CHECKING:
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.core.process import ProcessManager
    from claude_daemon.integrations.dashboard import DashboardHub
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)

# Pattern to detect agent addressing: @coder, /coder, or "Hey coder,"
AGENT_ADDRESS_PATTERN = re.compile(
    r'^(?:@|/)(\w+)\b\s*(.*)', re.DOTALL
)

# Placeholder names used in documentation — never match these as real agent names
_PLACEHOLDER = r'(?!(?:name|agent_name|agent|target|example)\b)'

# Common terminator for all dispatch tags. Extending this keeps the
# parsers consistent when new tag families (factory, etc.) are added.
_TAG_TERMINATORS = (
    r'\[DELEGATE:|\[DISCUSS:|\[COUNCIL\]|\[HELP:|\[OPTIMIZE:|'
    r'\[STATUS:|\[STATUS\]|'
    r'\[BUILD\]|\[PLAN\]|\[REVIEW\]|\Z'
)

# Pattern to detect delegation requests in agent responses: [DELEGATE:agent_name] message
DELEGATION_PATTERN = re.compile(
    rf'\[DELEGATE:{_PLACEHOLDER}(\w+)\]\s*(.*?)(?={_TAG_TERMINATORS})',
    re.DOTALL,
)

# [DISCUSS:agent_name] topic — request a bilateral discussion
DISCUSS_PATTERN = re.compile(
    rf'\[DISCUSS:{_PLACEHOLDER}(\w+)\]\s*(.*?)(?={_TAG_TERMINATORS})',
    re.DOTALL,
)

# [COUNCIL] topic — request a full council deliberation
COUNCIL_PATTERN = re.compile(
    rf'\[COUNCIL\]\s*(.*?)(?={_TAG_TERMINATORS})',
    re.DOTALL,
)

# [HELP:agent_name] question — quick single-turn consultation
HELP_PATTERN = re.compile(
    rf'\[HELP:{_PLACEHOLDER}(\w+)\]\s*(.*?)(?={_TAG_TERMINATORS})',
    re.DOTALL,
)

# [OPTIMIZE:agent_name] target — trigger evo code optimization workflow
OPTIMIZE_PATTERN = re.compile(
    rf'\[OPTIMIZE:{_PLACEHOLDER}(\w+)\]\s*(.*?)(?={_TAG_TERMINATORS})',
    re.DOTALL,
)

# [STATUS:agent_name] — query agent health/activity (no LLM call, pure data lookup)
STATUS_PATTERN = re.compile(
    rf'\[STATUS:{_PLACEHOLDER}(\w+)\]',
    re.DOTALL,
)

# [STATUS] (no agent name) — fleet-wide summary
STATUS_ALL_PATTERN = re.compile(
    r'\[STATUS\]',
)

# Software Factory dispatch tags — orchestrator emits these when a
# request warrants the plan/build/review loop. No agent name needed:
# role assignment is centralised in FactoryConfig.
BUILD_PATTERN = re.compile(
    rf'\[BUILD\]\s*(.*?)(?={_TAG_TERMINATORS})',
    re.DOTALL,
)
PLAN_PATTERN = re.compile(
    rf'\[PLAN\]\s*(.*?)(?={_TAG_TERMINATORS})',
    re.DOTALL,
)
REVIEW_PATTERN = re.compile(
    rf'\[REVIEW\]\s*(.*?)(?={_TAG_TERMINATORS})',
    re.DOTALL,
)

_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```', re.DOTALL)
# Single-line inline code spans — strip these too so prose like
# "use the `[BUILD]` tag" doesn't fire the factory.
_INLINE_CODE_RE = re.compile(r'`[^`\n]+`')


def _strip_code_blocks(text: str) -> str:
    """Remove fenced + inline code spans to prevent false tag matches
    in examples or documentation-style prose."""
    without_fenced = _CODE_BLOCK_RE.sub('', text)
    return _INLINE_CODE_RE.sub('', without_fenced)


ROUTING_PROMPT = """\
You are the orchestrator. A message has arrived that needs routing to the right agent.

{agent_summary}

User message: {message}

Which agent should handle this? Respond with ONLY the agent name (lowercase).
If you should handle it yourself, respond with: orchestrator
If no agent is a good fit, respond with: orchestrator
"""


@dataclass
class SpawnedTask:
    """A background task running on an agent."""

    task_id: str
    agent_name: str
    prompt: str
    status: str = "running"  # running, completed, failed
    result: str = ""
    cost: float = 0.0
    _future: asyncio.Task | None = field(default=None, repr=False)


class Orchestrator:
    """Routes messages to appropriate agents and manages inter-agent communication."""

    def __init__(
        self,
        registry: AgentRegistry,
        process_manager: ProcessManager,
        store: ConversationStore,
        hub: DashboardHub | None = None,
        failure_analyzer=None,
        embedding_store=None,
    ) -> None:
        self.registry = registry
        self.pm = process_manager
        self.store = store
        self.hub = hub
        self._failure_analyzer = failure_analyzer
        self._embedding_store = embedding_store
        self._discussion_engine = None
        self._workflow_engine = None
        self._factory = None  # SoftwareFactory — set post-init by daemon
        self._budget_store = None
        self._compactor = None
        self._spawned_tasks: dict[str, SpawnedTask] = {}

    def set_discussion_engine(self, engine) -> None:
        """Inject discussion engine (avoids circular init)."""
        self._discussion_engine = engine

    def set_workflow_engine(self, engine) -> None:
        """Inject workflow engine (avoids circular init)."""
        self._workflow_engine = engine

    def set_factory(self, factory) -> None:
        """Inject the Software Factory (avoids circular init).

        Enables [BUILD] / [PLAN] / [REVIEW] tag handling. When unset,
        factory tags emitted by agents are ignored (handlers no-op).
        """
        self._factory = factory

    def set_budget_store(self, budget_store) -> None:
        """Inject budget store for post-completion spend recording."""
        self._budget_store = budget_store

    def set_compactor(self, compactor) -> None:
        """Inject context compactor for post-conversation signal extraction."""
        self._compactor = compactor

    async def _semantic_search(self, prompt: str) -> list[dict]:
        """Hybrid search: semantic vector search with FTS5 keyword fallback.

        Returns combined results when semantic matches are sparse.
        """
        matches: list[dict] = []
        if self._embedding_store and self._embedding_store.available:
            try:
                matches = await self._embedding_store.search(prompt[:500])
            except Exception:
                pass

        # Hybrid fallback: supplement with FTS5 if semantic results are sparse
        if len(matches) < 2:
            try:
                fts_results = self.store.search_conversations(prompt[:200], limit=3)
                seen = {m["chunk"] for m in matches}
                for r in fts_results:
                    snippet = r["content"][:300]
                    if snippet not in seen:
                        matches.append({
                            "chunk": snippet,
                            "source": "conversation",
                            "agent_name": r.get("user_id", ""),
                            "score": 0.5,
                        })
            except Exception:
                pass

        return matches

    async def _extract_signals(self, session_id: str, agent_name: str) -> None:
        """Background: run light_sleep to extract memory signals from the conversation."""
        try:
            signals = await self._compactor.light_sleep(session_id)
            if signals:
                log.info("Memory signals extracted for %s: %d signals", agent_name, len(signals))
        except Exception:
            log.debug("Signal extraction failed for %s", agent_name, exc_info=True)

    def resolve_agent(self, message: str) -> tuple[Agent | None, str]:
        """Determine which agent should handle a message.

        Returns (agent, cleaned_message).
        If the message explicitly addresses an agent (@name or /name), route directly.
        Uses fuzzy matching for typos (e.g. @jony → johnny).
        Otherwise returns None (caller should use auto-routing).
        """
        match = AGENT_ADDRESS_PATTERN.match(message.strip())
        if match:
            agent_name = match.group(1).lower()
            remaining = match.group(2).strip()
            agent = self.registry.get(agent_name)
            if agent:
                return agent, remaining or message

            # Fuzzy match: suggest closest agent name
            names = self.registry.agent_names()
            close = difflib.get_close_matches(agent_name, names, n=1, cutoff=0.6)
            if close:
                fuzzy_agent = self.registry.get(close[0])
                if fuzzy_agent:
                    log.info("Fuzzy match: '%s' → '%s'", agent_name, close[0])
                    return fuzzy_agent, remaining or message

        return None, message

    async def auto_route(self, message: str) -> Agent | None:
        """Use the orchestrator agent to decide which agent handles a message.

        Falls back to orchestrator itself if routing fails.
        """
        orchestrator = self.registry.get_orchestrator()
        if not orchestrator:
            # No orchestrator - just use first agent or create one
            agents = self.registry.list_agents()
            return agents[0] if agents else None

        # If there's only the orchestrator, no need to route
        if len(self.registry) <= 1:
            return orchestrator

        agent_summary = self.registry.get_agent_summary()
        prompt = ROUTING_PROMPT.format(
            agent_summary=agent_summary,
            message=message[:500],
        )

        response = await self.pm.send_message(
            prompt=prompt,
            max_budget=0.02,  # Routing should be cheap
            platform="system",
            user_id="orchestrator",
        )

        if response.is_error:
            return orchestrator

        chosen_name = response.result.strip().lower().split("\n")[0].strip()
        chosen = self.registry.get(chosen_name)
        if chosen:
            log.info("Routed to agent: %s", chosen_name)
            return chosen

        return orchestrator

    def _check_agent_budget(self, agent_name: str) -> bool:
        """Check if agent has exceeded its daily budget. Returns True if within budget."""
        budget = self.pm.config.per_agent_daily_budget
        if budget <= 0:
            return True  # Unlimited
        metrics = self.store.get_agent_metrics(agent_name=agent_name, days=1)
        spent = sum(m.get("total_cost", 0) for m in metrics)
        if spent >= budget:
            log.warning("Agent %s over daily budget: $%.2f / $%.2f", agent_name, spent, budget)
            return False
        return True

    def _pick_resume_session(self, conv: dict) -> str | None:
        """Return the conv's SDK session_id if it's a fresh candidate for resume.

        Only returns a session_id when: the conversation has prior messages,
        the last activity is within `sdk_resume_max_age_hours`, and the daemon
        has sdk_resume_max_age_hours > 0.
        """
        max_age = getattr(self.pm.config, "sdk_resume_max_age_hours", 0)
        if max_age <= 0:
            return None
        sess_id = conv.get("session_id")
        if not sess_id:
            return None
        # Skip brand-new convs — nothing to resume.
        if not conv.get("message_count"):
            return None
        last_active = conv.get("last_active")
        if not last_active:
            return None
        try:
            if isinstance(last_active, datetime):
                ts = last_active if last_active.tzinfo else last_active.replace(
                    tzinfo=timezone.utc,
                )
            else:
                ts = datetime.fromisoformat(str(last_active).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age)
        return sess_id if ts > cutoff else None

    async def send_to_agent(
        self,
        agent: Agent,
        prompt: str,
        session_id: str | None = None,
        platform: str = "cli",
        user_id: str = "local",
        task_type: str = "default",
    ) -> ClaudeResponse:
        """Send a message to a specific agent with its full identity context."""
        # Per-agent daily budget check
        if not self._check_agent_budget(agent.name):
            budget = self.pm.config.per_agent_daily_budget
            return ClaudeResponse.error(
                f"Agent '{agent.name}' has exceeded its daily budget of ${budget:.2f}. "
                "Try again tomorrow or adjust per_agent_daily_budget in config."
            )

        correlation_id = str(uuid.uuid4())[:12]
        log.info("[%s] %s <- %s:%s prompt_len=%d", correlation_id, agent.name, platform, user_id, len(prompt))

        model = agent.get_model(task_type)
        mcp_path = agent.mcp_config_path

        # Launch semantic search + session ensure in parallel
        search_task = asyncio.create_task(self._semantic_search(prompt))
        ensure_task = asyncio.create_task(self.pm.ensure_agent_session(
            agent_name=agent.name,
            model=model,
            system_prompt=agent.build_static_context(),
            mcp_config_path=mcp_path,
            settings_path=agent.settings_path,
            agent_workspace=str(agent.workspace),
        ))

        conv = self.store.get_or_create_conversation(
            session_id=session_id,
            platform=platform,
            user_id=f"{user_id}:{agent.name}",
        )
        self.store.add_message(conv["id"], "user", prompt)

        await ensure_task
        semantic_matches: list[dict] = []
        timeout_s = self.pm.config.embedding_search_timeout_ms / 1000.0
        try:
            semantic_matches = await asyncio.wait_for(search_task, timeout=timeout_s)
        except asyncio.TimeoutError:
            log.debug("Semantic search exceeded %dms budget", self.pm.config.embedding_search_timeout_ms)
            search_task.cancel()

        agent_context = agent.build_system_context(semantic_matches=semantic_matches)
        agent_context += f"\n\n{self.registry.get_agent_summary()}"

        if self.hub:
            asyncio.create_task(self.hub.agent_busy(agent.name, prompt))

        sdk_active = (self.pm._sdk_bridge and
                      self.pm._sdk_bridge.has_session(agent.name, model))
        if sdk_active:
            dynamic_context = agent.build_dynamic_context(semantic_matches=semantic_matches)
            recent = self.store.get_conversation_text(conv["id"], limit=20)
            if recent:
                history = f"## Recent Conversation History\n{recent[-3000:]}"
                dynamic_context = f"{history}\n\n{dynamic_context}" if dynamic_context else history
            effective_context = dynamic_context or None
        else:
            effective_context = agent_context

        response = await self.pm.send_message(
            prompt=prompt,
            session_id=conv["session_id"],
            system_context=effective_context,
            platform=platform,
            user_id=user_id,
            model_override=model,
            mcp_config_path=mcp_path,
            settings_path=agent.settings_path,
            effort=agent.get_effort(task_type),
            task_type=task_type,
            agent_name=agent.name,
        )

        if self.hub:
            await self.hub.agent_idle(agent.name, response.cost, response.duration_ms)

        self.store.add_message(
            conv["id"], "assistant", response.result,
            tokens=response.output_tokens, cost=response.cost,
        )
        # If auto-parallel created a fresh session, don't overwrite the primary
        # session pointer — keep it resumable for the next non-parallel message.
        auto_parallel = response.session_id and response.session_id != conv["session_id"]
        if auto_parallel:
            self.store.update_conversation(conv["id"], cost=response.cost)
            if self.hub:
                await self.hub.auto_parallel(agent.name, response.session_id)
        else:
            self.store.update_conversation(
                conv["id"], session_id=response.session_id, cost=response.cost,
            )

        # Record per-agent metrics
        self.store.record_agent_metric(
            agent_name=agent.name, metric_type="message",
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost,
            duration_ms=response.duration_ms,
            model=model, platform=platform,
            success=not response.is_error,
        )

        log.info("[%s] %s -> cost=$%.4f tokens=%d/%d", correlation_id, agent.name, response.cost, response.input_tokens, response.output_tokens)

        # Audit log
        self.store.record_audit(
            action="agent_message", agent_name=agent.name,
            user_id=user_id, platform=platform,
            details=f"prompt_len={len(prompt)}, result_len={len(response.result)}, model={model}",
            cost_usd=response.cost, success=not response.is_error,
        )

        # Analyze failures for lesson extraction
        if response.is_error and self._failure_analyzer:
            try:
                asyncio.create_task(
                    self._failure_analyzer.analyze(
                        agent.name, task_type, response.result[:1500],
                    )
                )
            except Exception:
                pass

        # Extract memory signals in background (feed the light_sleep → deep_sleep → MEMORY.md pipeline)
        if not response.is_error and response.output_tokens >= 50 and getattr(self, "_compactor", None):
            asyncio.create_task(self._extract_signals(conv["session_id"], agent.name))

        # Process delegation tags in response (skip discussion tags when inside a discussion)
        if not response.is_error:
            response = await self._process_delegations(agent, response, platform=platform)

        return response

    async def _process_delegations(
        self, from_agent: Agent, response: ClaudeResponse,
        platform: str = "cli",
    ) -> ClaudeResponse:
        """Scan agent response for [DELEGATE:name] tags and execute inter-agent calls.

        Appends delegation results to the response text.
        Skips discussion/council/help tags when platform='discussion' to prevent recursion.
        """
        # Strip code blocks to avoid matching example tags in markdown
        scan_text = _strip_code_blocks(response.result)
        delegations = DELEGATION_PATTERN.findall(scan_text)

        appended = []
        for target_name, message in delegations:
            target = self.registry.get(target_name.lower())
            if not target:
                appended.append(f"\n[Delegation to '{target_name}' failed: agent not found]")
                continue

            log.info("Delegation: %s -> %s", from_agent.name, target_name)
            self.store.record_audit(
                action="agent_delegation", agent_name=from_agent.name,
                details=f"delegated to {target_name}: {message.strip()[:200]}",
            )
            try:
                result = await self.agent_to_agent(
                    from_agent, target, message.strip(),
                )
                appended.append(
                    f"\n\n--- Response from {target.identity.display_name} ---\n{result}"
                )
            except Exception:
                log.exception("Delegation from %s to %s failed", from_agent.name, target_name)
                appended.append(f"\n[Delegation to '{target_name}' failed: error]")

        if appended:
            response.result += "\n".join(appended)

        # STATUS queries are always allowed — pure data, no LLM cost, no recursion risk
        response = await self._process_status_queries(from_agent, response)

        # Skip discussion/help/council/optimize tags inside discussion turns (prevent recursion)
        if platform not in ("discussion", "council", "intercom"):
            response = await self._process_help_requests(from_agent, response)
            if self._discussion_engine:
                response = await self._process_discussions(from_agent, response)
                response = await self._process_councils(from_agent, response)
            if self._workflow_engine:
                response = await self._process_optimizations(from_agent, response)
            if self._factory:
                response = await self._process_factory_requests(
                    from_agent, response, platform=platform,
                )

        return response

    async def _process_help_requests(
        self, from_agent: Agent, response: ClaudeResponse,
    ) -> ClaudeResponse:
        """Process [HELP:name] tags — quick single-turn consultation."""
        helps = HELP_PATTERN.findall(_strip_code_blocks(response.result))
        if not helps:
            return response

        appended = []
        for target_name, question in helps:
            target = self.registry.get(target_name.lower())
            if not target:
                appended.append(f"\n[Help from '{target_name}' failed: agent not found]")
                continue

            log.info("Help request: %s -> %s", from_agent.name, target_name)
            self.store.record_audit(
                action="agent_help_request", agent_name=from_agent.name,
                details=f"help from {target_name}: {question.strip()[:200]}",
            )
            try:
                result = await self.agent_to_agent(
                    from_agent, target,
                    f"[Help request from {from_agent.name}]\n\n{question.strip()}",
                )
                appended.append(
                    f"\n\n--- Help from {target.identity.display_name} ---\n{result}"
                )
            except Exception:
                log.exception("Help request %s -> %s failed", from_agent.name, target_name)
                appended.append(f"\n[Help from '{target_name}' failed: error]")

        if appended:
            response.result += "\n".join(appended)
        return response

    async def _process_status_queries(
        self, from_agent: Agent, response: ClaudeResponse,
    ) -> ClaudeResponse:
        """Process [STATUS:name] and [STATUS] tags — pure data lookup, no LLM call."""
        scan_text = _strip_code_blocks(response.result)

        appended = []

        # Per-agent status queries
        for target_name in STATUS_PATTERN.findall(scan_text):
            target = self.registry.get(target_name.lower())
            if not target:
                appended.append(f"\n[Status query for '{target_name}' failed: agent not found]")
                continue

            active = self.pm.active_count_for_agent(target.name)
            metrics = self.store.get_agent_metrics(agent_name=target.name, days=1)
            mcp = target.check_mcp_health()
            mcp_ok = sum(1 for v in mcp.values() if v == "configured")
            mcp_total = len(mcp)

            total_cost = sum(m.get("total_cost", 0) for m in metrics)
            total_msgs = sum(m.get("count", 0) for m in metrics)

            mcp_str = f"{mcp_ok}/{mcp_total} ok" if mcp_total else "none"
            appended.append(
                f"\n\n--- Status: {target.identity.display_name} ---\n"
                f"Role: {target.identity.role} | Model: {target.get_model('default')}\n"
                f"Sessions: {active} active | MCP: {mcp_str}\n"
                f"Today: {total_msgs} messages, ${total_cost:.4f}"
            )

        # Fleet-wide status queries
        if STATUS_ALL_PATTERN.search(scan_text):
            lines = ["--- Fleet Status ---"]
            total_active = 0
            total_cost = 0.0
            for agent in self.registry:
                active = self.pm.active_count_for_agent(agent.name)
                metrics = self.store.get_agent_metrics(agent_name=agent.name, days=1)
                cost = sum(m.get("total_cost", 0) for m in metrics)
                msgs = sum(m.get("count", 0) for m in metrics)
                mcp = agent.check_mcp_health()
                mcp_ok = sum(1 for v in mcp.values() if v == "configured")
                mcp_total = len(mcp)
                mcp_str = f"{mcp_ok}/{mcp_total}" if mcp_total else "-"
                lines.append(
                    f"{agent.name:<16} | {active} active | "
                    f"${cost:.2f} ({msgs} msgs) | MCP {mcp_str}"
                )
                total_active += active
                total_cost += cost
            lines.append(f"Total: {total_active} active sessions | ${total_cost:.2f} today")
            appended.append("\n\n" + "\n".join(lines))

        if appended:
            response.result += "\n".join(appended)
        return response

    async def _process_discussions(
        self, from_agent: Agent, response: ClaudeResponse,
    ) -> ClaudeResponse:
        """Process [DISCUSS:name] tags — launch bilateral discussions."""
        discussions = DISCUSS_PATTERN.findall(_strip_code_blocks(response.result))
        if not discussions:
            return response

        appended = []
        for target_name, topic in discussions:
            target = self.registry.get(target_name.lower())
            if not target:
                appended.append(f"\n[Discussion with '{target_name}' failed: agent not found]")
                continue

            log.info("Discussion: %s <-> %s on: %s", from_agent.name, target_name, topic[:80])
            self.store.record_audit(
                action="agent_discussion", agent_name=from_agent.name,
                details=f"bilateral with {target_name}: {topic.strip()[:200]}",
            )
            try:
                result = await self._discussion_engine.run_bilateral(
                    agent_a=from_agent.name,
                    agent_b=target_name.lower(),
                    topic=topic.strip(),
                )
                summary = result.synthesis or (
                    result.turns[-1].content if result.turns else "No conclusion"
                )
                appended.append(
                    f"\n\n--- Discussion with {target.identity.display_name} ---\n"
                    f"Outcome: {result.outcome} | "
                    f"Cost: ${result.total_cost:.4f} | "
                    f"Turns: {len(result.turns)}\n\n{summary}"
                )
            except Exception:
                log.exception("Discussion %s <-> %s failed", from_agent.name, target_name)
                appended.append(f"\n[Discussion with '{target_name}' failed: error]")

        if appended:
            response.result += "\n".join(appended)
        return response

    async def _process_councils(
        self, from_agent: Agent, response: ClaudeResponse,
    ) -> ClaudeResponse:
        """Process [COUNCIL] tags — launch full council deliberation."""
        councils = COUNCIL_PATTERN.findall(_strip_code_blocks(response.result))
        if not councils:
            return response

        appended = []
        for topic in councils:
            log.info("Council convened by %s on: %s", from_agent.name, topic[:80])
            self.store.record_audit(
                action="council_session", agent_name=from_agent.name,
                details=f"council: {topic.strip()[:200]}",
            )
            try:
                result = await self._discussion_engine.run_council(
                    topic=topic.strip(),
                )
                action_line = ""
                # Check if any tasks were spawned (stored on the DB row)
                try:
                    import json as _json
                    disc_row = self.store.get_discussion(result.discussion_id)
                    if disc_row:
                        raw = disc_row.get("action_task_ids") or "[]"
                        ids = _json.loads(raw)
                        if ids:
                            action_line = (
                                f"\nActions spawned: {len(ids)} "
                                f"(see Operations tab, source=council)"
                            )
                except Exception:
                    pass
                if not action_line:
                    action_line = "\nNo auto-executable actions extracted."
                appended.append(
                    f"\n\n--- Council Decision ---\n"
                    f"Outcome: {result.outcome} | "
                    f"Cost: ${result.total_cost:.4f} | "
                    f"Participants: {', '.join(result.config.participants)}\n"
                    f"View transcript: /#discussions/{result.discussion_id}"
                    f"{action_line}\n\n"
                    f"{result.synthesis}"
                )
            except Exception:
                log.exception("Council session failed")
                appended.append("\n[Council session failed: error]")

        if appended:
            response.result += "\n".join(appended)
        return response

    async def _process_optimizations(
        self, from_agent: Agent, response: ClaudeResponse,
    ) -> ClaudeResponse:
        """Process [OPTIMIZE:agent_name] tags — trigger evo code optimization."""
        optimizations = OPTIMIZE_PATTERN.findall(_strip_code_blocks(response.result))
        if not optimizations:
            return response

        appended = []
        for target_name, description in optimizations:
            target = self.registry.get(target_name.lower())
            if not target:
                appended.append(f"\n[Optimization for '{target_name}' failed: agent not found]")
                continue

            log.info("Optimization: %s -> %s: %s", from_agent.name, target_name, description[:80])
            self.store.record_audit(
                action="evo_optimization", agent_name=from_agent.name,
                details=f"optimize via {target_name}: {description.strip()[:200]}",
            )
            try:
                result = await self._workflow_engine.execute_optimization(
                    agent_name=target_name.lower(),
                    target=description.strip(),
                )
                appended.append(
                    f"\n\n--- Optimization Result ({target.identity.display_name}) ---\n"
                    f"{result.final_result[:1000]}"
                )
            except Exception:
                log.exception("Optimization %s -> %s failed", from_agent.name, target_name)
                appended.append(f"\n[Optimization for '{target_name}' failed: error]")

        if appended:
            response.result += "\n".join(appended)
        return response

    async def _process_factory_requests(
        self, from_agent: Agent, response: ClaudeResponse,
        platform: str = "cli",
    ) -> ClaudeResponse:
        """Process [BUILD] / [PLAN] / [REVIEW] tags — dispatch to the
        Software Factory.

        Design notes (from audit):
        - Called inside the platform guard so factory loops cannot be
          retriggered from discussion/council turns.
        - No agent name in the tags — role assignment lives in
          FactoryConfig, not in the tag.
        """
        if not self._factory:
            return response

        scan = _strip_code_blocks(response.result)
        # Cap fan-out so a glitchy / adversarial agent turn can't fire
        # dozens of concurrent factory pipelines in one response.
        max_per_turn = 3
        builds = BUILD_PATTERN.findall(scan)[:max_per_turn]
        plans = PLAN_PATTERN.findall(scan)[:max_per_turn]
        reviews = REVIEW_PATTERN.findall(scan)[:max_per_turn]
        if not (builds or plans or reviews):
            return response

        appended: list[str] = []

        for description in plans:
            desc = description.strip()
            if not desc:
                continue
            log.info("Factory PLAN: %s -> %s", from_agent.name, desc[:80])
            self.store.record_audit(
                action="factory_plan_tag", agent_name=from_agent.name,
                details=f"plan: {desc[:200]}", platform=platform,
            )
            try:
                result = await self._factory.plan(
                    desc, platform=platform, user_id=from_agent.name,
                )
                snippet = result.plan_content[:1200]
                status_line = (
                    f"status={result.status}"
                    + (f" approval_id={result.approval_id}" if result.approval_id else "")
                    + (f" plan={result.plan_path}" if result.plan_path else "")
                )
                appended.append(
                    f"\n\n--- Plan Created ({status_line}) ---\n{snippet}"
                )
            except Exception:
                log.exception("Factory PLAN failed for %s", from_agent.name)
                appended.append("\n[Factory PLAN failed: error]")

        for description in builds:
            desc = description.strip()
            if not desc:
                continue
            log.info("Factory BUILD: %s -> %s", from_agent.name, desc[:80])
            self.store.record_audit(
                action="factory_build_tag", agent_name=from_agent.name,
                details=f"build: {desc[:200]}", platform=platform,
            )
            try:
                result = await self._factory.build(
                    desc, platform=platform, user_id=from_agent.name,
                )
                appended.append(
                    f"\n\n--- Build Result (slug={result.slug}) ---\n"
                    f"{result.summary}"
                )
            except Exception:
                log.exception("Factory BUILD failed for %s", from_agent.name)
                appended.append("\n[Factory BUILD failed: error]")

        for target in reviews:
            tgt = target.strip()
            log.info("Factory REVIEW: %s -> %s", from_agent.name, tgt[:80])
            self.store.record_audit(
                action="factory_review_tag", agent_name=from_agent.name,
                details=f"review: {tgt[:200]}", platform=platform,
            )
            try:
                result = await self._factory.review(
                    tgt or None, platform=platform, user_id=from_agent.name,
                )
                appended.append(
                    f"\n\n--- Review Report (slug={result.slug}) ---\n"
                    f"{result.summary}"
                )
            except Exception:
                log.exception("Factory REVIEW failed for %s", from_agent.name)
                appended.append("\n[Factory REVIEW failed: error]")

        if appended:
            response.result += "\n".join(appended)
        return response

    async def stream_to_agent(
        self,
        agent: Agent,
        prompt: str,
        session_id: str | None = None,
        platform: str = "cli",
        user_id: str = "local",
        task_type: str = "default",
    ) -> AsyncIterator[str | ClaudeResponse]:
        """Stream a message to a specific agent."""
        model = agent.get_model(task_type)
        mcp_path = agent.mcp_config_path

        # Launch semantic search + session ensure in parallel to minimise
        # time-to-first-token. Semantic search is best-effort with a timeout.
        interactive = platform in ("api", "cli")
        skip_search = interactive and not self.pm.config.embedding_interactive_chat
        if skip_search:
            search_task = None
        else:
            search_task = asyncio.create_task(self._semantic_search(prompt))

        ensure_task = asyncio.create_task(self.pm.ensure_agent_session(
            agent_name=agent.name,
            model=model,
            system_prompt=agent.build_static_context(),
            mcp_config_path=mcp_path,
            settings_path=agent.settings_path,
            agent_workspace=str(agent.workspace),
        ))

        conv = self.store.get_or_create_conversation(
            session_id=session_id,
            platform=platform,
            user_id=f"{user_id}:{agent.name}",
        )
        self.store.add_message(conv["id"], "user", prompt)

        # Await session ensure (usually instant for warm sessions)
        await ensure_task

        # Await semantic search with a hard timeout so a slow embedding
        # provider never blocks the chat turn.
        semantic_matches: list[dict] = []
        if search_task is not None:
            timeout_s = self.pm.config.embedding_search_timeout_ms / 1000.0
            try:
                semantic_matches = await asyncio.wait_for(search_task, timeout=timeout_s)
            except asyncio.TimeoutError:
                log.debug("Semantic search exceeded %dms budget — proceeding without", self.pm.config.embedding_search_timeout_ms)
                search_task.cancel()

        agent_context = agent.build_system_context(semantic_matches=semantic_matches)
        agent_context += f"\n\n{self.registry.get_agent_summary()}"

        accumulated = ""
        final_response = None
        resp = ClaudeResponse.error("No response")

        # Persist chat task row in background (fire-and-forget)
        chat_task_id: str | None = None
        if self.store is not None:
            try:
                chat_task_id = uuid.uuid4().hex[:12]
                self.store.create_task(
                    chat_task_id, agent.name, prompt[:2000],
                    task_type=task_type, platform=platform, user_id=user_id,
                    source="chat", initial_status="running",
                    session_id=conv["session_id"],
                )
                if self.hub:
                    try:
                        asyncio.create_task(
                            self.hub.task_created(chat_task_id, agent.name, prompt[:200]),
                        )
                    except Exception:
                        pass
            except Exception:
                log.debug("Could not persist chat task row")
                chat_task_id = None

        if self.hub:
            asyncio.create_task(self.hub.agent_busy(agent.name, prompt))

        try:
            sdk_active = (self.pm._sdk_bridge and
                          self.pm._sdk_bridge.has_session(agent.name, model))
            if sdk_active:
                dynamic_context = agent.build_dynamic_context(semantic_matches=semantic_matches)
                recent = self.store.get_conversation_text(conv["id"], limit=20)
                if recent:
                    history = f"## Recent Conversation History\n{recent[-3000:]}"
                    dynamic_context = f"{history}\n\n{dynamic_context}" if dynamic_context else history
                effective_context = dynamic_context or None
            else:
                effective_context = agent_context

            async for chunk in self.pm.stream_message(
                prompt=prompt,
                session_id=conv["session_id"],
                system_context=effective_context,
                platform=platform,
                user_id=user_id,
                model_override=model,
                mcp_config_path=mcp_path,
                settings_path=agent.settings_path,
                effort=agent.get_effort(task_type),
                task_type=task_type,
                agent_name=agent.name,
            ):
                if isinstance(chunk, str):
                    accumulated += chunk
                    if self.hub:
                        await self.hub.stream_delta(agent.name, chunk)
                    yield chunk
                elif isinstance(chunk, ClaudeResponse):
                    final_response = chunk

            resp = final_response or ClaudeResponse.error("No response")
        except Exception:
            log.exception("stream_to_agent error for %s", agent.name)
            resp = ClaudeResponse.error("Agent error")
        finally:
            # Classify "no usable response" more precisely now that stop_reason
            # flows through. A clean end_turn with empty accumulated text is
            # legitimate (tool-only turn, refusal, etc.) and shouldn't spam
            # as a warning; only genuine infra errors or non-end stops do.
            stop_reason = getattr(resp, "stop_reason", "") or ""
            empty_without_error = (
                not resp.is_error and not accumulated and stop_reason not in {"end_turn", ""}
            )
            if resp.is_error or empty_without_error:
                log.warning(
                    "Agent %s produced no usable response. is_error=%s "
                    "stop_reason=%s result=%r",
                    agent.name, resp.is_error, stop_reason or "unknown",
                    (resp.result or "")[:200],
                )
            # Refund cost on infra failures — the user shouldn't pay for an
            # SDK-bridge timeout or a stream that ended without a result.
            recorded_cost = resp.cost or 0.0
            if resp.is_error and resp.result and any(
                marker in resp.result for marker in (
                    "idle timeout",
                    "stream ended without result",
                    "bridge process may have crashed",
                )
            ):
                if recorded_cost > 0:
                    log.warning(
                        "Refunding $%.4f for infra failure on agent %s: %s",
                        recorded_cost, agent.name, (resp.result or "")[:120],
                    )
                recorded_cost = 0.0
            final_session = getattr(resp, "session_id", None) or conv["session_id"]
            if chat_task_id and self.store is not None:
                try:
                    if resp.is_error:
                        self.store.update_task_status(
                            chat_task_id, "failed",
                            error=resp.result or "Agent error",
                            cost_usd=recorded_cost,
                            session_id=final_session,
                        )
                    else:
                        self.store.update_task_status(
                            chat_task_id, "completed",
                            result=(accumulated or resp.result or "")[:4000],
                            cost_usd=resp.cost or 0.0,
                            session_id=final_session,
                        )
                    if self.hub:
                        try:
                            asyncio.create_task(
                                self.hub.task_update(
                                    chat_task_id, agent.name,
                                    "failed" if resp.is_error else "completed",
                                    cost=recorded_cost,
                                ),
                            )
                        except Exception:
                            pass
                except Exception:
                    log.debug("Could not update chat task row %s", chat_task_id)
            if self.hub:
                await self.hub.agent_idle(agent.name, resp.cost, resp.duration_ms)

        self.store.add_message(
            conv["id"], "assistant", accumulated or resp.result,
            tokens=resp.output_tokens, cost=resp.cost,
        )
        # Preserve primary session pointer if auto-parallel created a fresh session
        auto_parallel = resp.session_id and resp.session_id != conv["session_id"]
        if auto_parallel:
            self.store.update_conversation(conv["id"], cost=resp.cost)
            if self.hub:
                await self.hub.auto_parallel(agent.name, resp.session_id)
        else:
            self.store.update_conversation(
                conv["id"], session_id=resp.session_id, cost=resp.cost,
            )

        self.store.record_agent_metric(
            agent_name=agent.name, metric_type="stream",
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cost_usd=resp.cost,
            duration_ms=resp.duration_ms,
            model=model, platform=platform,
            success=not resp.is_error,
        )

        # Extract memory signals in background
        if not resp.is_error and resp.output_tokens >= 50 and getattr(self, "_compactor", None):
            asyncio.create_task(self._extract_signals(conv["session_id"], agent.name))

        yield resp

    async def agent_to_agent(
        self,
        from_agent: Agent,
        to_agent: Agent,
        message: str,
        task_type: str = "default",
    ) -> str:
        """Enable one agent to send a message to another.

        The receiving agent sees the message as coming from the sending agent.
        """
        prompt = (
            f"[Message from agent '{from_agent.name}']\n\n{message}"
        )
        response = await self.send_to_agent(
            agent=to_agent,
            prompt=prompt,
            platform="intercom",
            user_id=f"agent:{from_agent.name}",
            task_type=task_type,
        )
        return response.result

    # -- Parallel task dispatch --

    # Completed tasks are kept for this many seconds before cleanup
    _TASK_TTL_SECONDS = 3600  # 1 hour

    def spawn_task(
        self,
        agent: Agent,
        prompt: str,
        platform: str = "spawn",
        user_id: str = "local",
        task_type: str = "default",
        task_id: str | None = None,
    ) -> SpawnedTask:
        """Launch a task on an agent in the background (non-blocking).

        Each spawned task gets its own fresh session so multiple tasks
        to the same agent run truly in parallel (no --resume collision).
        Returns immediately with a SpawnedTask for tracking.

        If ``task_id`` is provided, it's used as-is and the DB row is assumed
        to already exist (created by an external caller via TaskAPI). If None,
        a fresh id is generated and the row is created here.
        """
        # Cleanup old completed tasks to prevent memory leak
        self._cleanup_finished_tasks()

        pre_persisted = task_id is not None
        if task_id is None:
            task_id = str(uuid.uuid4())[:12]

        # Persist to SQLite so tasks survive daemon restarts (unless already done)
        if not pre_persisted:
            try:
                self.store.create_task(
                    task_id, agent.name, prompt[:2000],
                    task_type=task_type, platform=platform, user_id=user_id,
                    source="spawn",
                )
            except Exception:
                log.debug("Could not persist task %s to DB", task_id)

        # Broadcast task creation so the dashboard updates immediately
        if self.hub:
            try:
                asyncio.create_task(
                    self.hub.task_created(task_id, agent.name, prompt[:200]),
                )
            except Exception:
                pass

        async def _run():
            try:
                self.store.update_task_status(task_id, "running")
            except Exception:
                pass
            try:
                # Use a unique session key to avoid sharing sessions
                unique_user = f"{user_id}:spawn:{task_id}"
                response = await self.send_to_agent(
                    agent=agent,
                    prompt=prompt,
                    platform=platform,
                    user_id=unique_user,
                    task_type=task_type,
                )
                spawned.result = response.result
                spawned.cost = response.cost
                spawned.status = "failed" if response.is_error else "completed"
            except Exception as e:
                log.exception("Background task %s on %s failed", task_id, agent.name)
                spawned.result = f"Task failed: {e}"
                spawned.status = "failed"
                response = None
            # Update DB with final status
            try:
                spawn_session = getattr(response, "session_id", None) if response else None
                self.store.update_task_status(
                    task_id, spawned.status,
                    result=spawned.result[:5000] if spawned.result else None,
                    error=spawned.result if spawned.status == "failed" else None,
                    cost_usd=spawned.cost,
                    session_id=spawn_session,
                )
            except Exception:
                pass
            # Reconcile budget reservations with actual cost
            if self._budget_store is not None:
                try:
                    import json as _json
                    task_row = self.store.get_task(task_id)
                    meta = {}
                    if task_row and task_row.get("metadata"):
                        meta = _json.loads(task_row["metadata"])
                    reservations = [
                        (int(bid), float(amt))
                        for bid, amt in meta.get("_budget_reservations", [])
                    ]
                    if reservations:
                        if spawned.status == "completed" and spawned.cost > 0:
                            updated = self._budget_store.apply_actual_spend(
                                reservations, spawned.cost,
                            )
                        else:
                            self._budget_store.release_reservations(reservations)
                            updated = []
                        if self.hub:
                            for b in updated:
                                await self.hub.budget_update(
                                    b["id"], b["scope"], b.get("scope_value"),
                                    b["current_spend"], b["limit_usd"],
                                )
                except Exception:
                    log.debug("Budget reconciliation failed for task %s", task_id)
            if self.hub:
                try:
                    await self.hub.task_update(
                        task_id, agent.name, spawned.status,
                        result=spawned.result, cost=spawned.cost,
                    )
                except Exception:
                    pass

        spawned = SpawnedTask(
            task_id=task_id,
            agent_name=agent.name,
            prompt=prompt[:200],
        )
        spawned._future = asyncio.create_task(_run())
        self._spawned_tasks[task_id] = spawned
        log.info("Spawned task %s on %s", task_id, agent.name)
        return spawned

    def _cleanup_finished_tasks(self) -> None:
        """Remove completed/failed tasks older than TTL to prevent memory leak."""
        to_remove = []
        for tid, task in self._spawned_tasks.items():
            if task.status in ("completed", "failed"):
                # Use future's done time if available
                if task._future and task._future.done():
                    to_remove.append(tid)
        # Keep at most 100 completed tasks, remove oldest beyond that
        if len(to_remove) > 100:
            for tid in to_remove[:len(to_remove) - 100]:
                del self._spawned_tasks[tid]

    async def spawn_parallel(
        self,
        tasks: list[tuple[Agent, str]],
        platform: str = "spawn",
        user_id: str = "local",
        task_type: str = "default",
    ) -> list[SpawnedTask]:
        """Launch multiple tasks in parallel and wait for all to complete.

        Each (agent, prompt) pair runs in its own session concurrently.
        Returns list of completed SpawnedTasks.
        """
        spawned = [
            self.spawn_task(agent, prompt, platform, user_id, task_type)
            for agent, prompt in tasks
        ]
        # Wait for all to complete
        await asyncio.gather(
            *[s._future for s in spawned if s._future],
            return_exceptions=True,
        )
        return spawned

    def get_task(self, task_id: str) -> SpawnedTask | None:
        return self._spawned_tasks.get(task_id)

    def list_tasks(self, status: str | None = None) -> list[SpawnedTask]:
        tasks = list(self._spawned_tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks
