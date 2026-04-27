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
    r'\[STATUS:|\[STATUS\]|\[SPAWN:|\[TASK:self\]|'
    r'\[BUILD\]|\[PLAN\]|\[REVIEW\]|\Z'
)

# Pattern to detect delegation requests in agent responses:
#   [DELEGATE:agent_name] message              — default routing (sonnet)
#   [DELEGATE:agent_name:complex] message      — planning routing (opus)
# The optional ``:flag`` group is mapped to a task_type via _resolve_task_type().
DELEGATION_PATTERN = re.compile(
    rf'\[DELEGATE:{_PLACEHOLDER}(\w+)(?::(\w+))?\]\s*(.*?)(?={_TAG_TERMINATORS})',
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

# [HELP:agent_name] question — quick single-turn consultation.
# Optional flag mirrors DELEGATION_PATTERN so a help-request can opt into
# opus when the consultation itself is complex.
HELP_PATTERN = re.compile(
    rf'\[HELP:{_PLACEHOLDER}(\w+)(?::(\w+))?\]\s*(.*?)(?={_TAG_TERMINATORS})',
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

# [SPAWN:agent_name] prompt — async background task (fire-and-forget)
SPAWN_PATTERN = re.compile(
    rf'\[SPAWN:{_PLACEHOLDER}(\w+)\]\s*(.*?)(?={_TAG_TERMINATORS})',
    re.DOTALL,
)

# [TASK:self] description — add to own pending task queue
SELF_TASK_PATTERN = re.compile(
    rf'\[TASK:self\]\s*(.*?)(?={_TAG_TERMINATORS})',
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


# Maps the optional delegation flag to the agent task_type used by
# ``agent.get_model(task_type)``. Any unknown flag falls back to
# ``"default"`` and emits a WARNING so typos surface in logs.
# See shared/playbooks/model-routing.md for the rubric.
_DELEGATION_FLAG_TO_TASK_TYPE = {
    "simple": "default",
    "complex": "planning",
    "plan": "planning",
    "planning": "planning",
}


def _resolve_task_type(flag: str | None) -> str:
    """Resolve a [DELEGATE:agent:flag] flag to an agent task_type.

    No flag (or empty) → ``"default"``. Unknown flag → ``"default"`` plus a
    WARNING so typos (e.g. ``[DELEGATE:albert:complx]``) are visible.
    """
    if not flag:
        return "default"
    task_type = _DELEGATION_FLAG_TO_TASK_TYPE.get(flag.lower())
    if task_type is None:
        log.warning(
            "Unknown delegation flag '%s' — falling back to 'default' "
            "(valid flags: %s)",
            flag, ", ".join(sorted(_DELEGATION_FLAG_TO_TASK_TYPE)),
        )
        return "default"
    return task_type


# Keywords whose presence in the original prompt pushes a task toward opus
# per the model-routing rubric's "when in doubt" clause.
_COMPLEXITY_KEYWORDS = (
    "refactor", "migrate", "design", "architecture",
    "implement new", "split", "consolidate", "rewrite",
)

# Regex to parse Albert/agents' wrap-up deliverables: "files_changed: N".
_FILES_CHANGED_RE = re.compile(
    r'files[_\s-]*changed\s*[:=]\s*(\d+)', re.IGNORECASE,
)
_LOC_DELTA_RE = re.compile(
    r'loc[_\s-]*delta\s*[:=]\s*(\d+)', re.IGNORECASE,
)
_NEW_TESTS_RE = re.compile(
    r'new[_\s-]*tests?[_\s-]*(?:added|files?)\s*[:=]\s*(yes|true|1|no|false|0)',
    re.IGNORECASE,
)


def _classify_model(model: str) -> str:
    """Collapse a resolved model name to 'opus' / 'sonnet' / 'other'."""
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return m or "unknown"


def _parse_delegation_result(result: str) -> tuple[int, int, bool]:
    """Best-effort parse of files_changed / loc_delta / new_tests_added
    from a delegated agent's wrap-up message. Missing fields → zeros/False.
    """
    files_changed = 0
    loc_delta = 0
    new_tests = False
    if not result:
        return files_changed, loc_delta, new_tests
    m = _FILES_CHANGED_RE.search(result)
    if m:
        try:
            files_changed = int(m.group(1))
        except ValueError:
            pass
    m = _LOC_DELTA_RE.search(result)
    if m:
        try:
            loc_delta = int(m.group(1))
        except ValueError:
            pass
    m = _NEW_TESTS_RE.search(result)
    if m:
        new_tests = m.group(1).lower() in ("yes", "true", "1")
    return files_changed, loc_delta, new_tests


def compute_delegation_audit(
    files_changed: int,
    loc_delta: int,
    new_test_files: bool,
    task_type: str,
    model_used: str,
    prompt: str = "",
) -> dict:
    """Apply the model-routing rubric to a completed delegation.

    Returns a dict with: ``tripped`` (int), ``expected`` ('opus'|'sonnet'),
    ``used`` (classified model), and ``outcome`` (one of
    'appropriate', 'under-routed', 'over-routed'). Pure function — safe to
    test without touching the DB.
    """
    prompt_lc = (prompt or "").lower()
    keyword_hit = any(kw in prompt_lc for kw in _COMPLEXITY_KEYWORDS)

    tripped = sum([
        files_changed >= 3,
        loc_delta >= 100,
        bool(new_test_files),
        task_type == "planning" or keyword_hit,
    ])

    expected = "opus" if tripped >= 2 else "sonnet"
    used = _classify_model(model_used)

    if used == expected:
        outcome = "appropriate"
    elif expected == "sonnet" and used == "opus":
        outcome = "under-routed"
    elif expected == "opus" and used == "sonnet":
        outcome = "over-routed"
    else:
        # e.g. haiku in either column — treat as informational mismatch.
        outcome = "appropriate"

    return {
        "tripped": tripped,
        "expected": expected,
        "used": used,
        "outcome": outcome,
    }


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
        self._events_lock = asyncio.Lock()
        self._delegate_tip_sent: set[str] = set()  # agents already shown the criteria tip

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

    async def _self_reflect(
        self, agent: Agent, prompt: str, result: str, conv_id: int,
    ) -> None:
        """Background: agent self-assesses its response quality.

        One lightweight LLM call (Haiku) checks: Did I fully address the
        request? Did I include evidence? What's still open? Stored in
        memory_summaries for the next heartbeat to surface.
        """
        try:
            reflect_prompt = (
                f"You just responded to this request:\n{prompt[:300]}\n\n"
                f"Your response summary:\n{result[:500]}\n\n"
                f"Self-assess in ONE line: Did you fully complete the task? "
                f"Did you include tests/evidence? What (if anything) is still open? "
                f"If everything is done, say COMPLETE. Otherwise list what remains."
            )
            response = await self.pm.send_message(
                prompt=reflect_prompt, max_budget=0.02,
                platform="system", user_id=f"reflect:{agent.name}",
                model_override="haiku",
            )
            if not response.is_error and response.result.strip():
                reflection = f"[{agent.name}] {response.result.strip()[:300]}"
                self.store.add_summary(conv_id, reflection, "reflection")
                if "COMPLETE" not in response.result.upper():
                    log.info("Self-reflection for %s: incomplete — %s", agent.name, response.result[:100])
        except Exception:
            log.debug("Self-reflection failed for %s", agent.name, exc_info=True)

    async def _index_conversation(self, conv_id: int, agent_name: str) -> None:
        """Background: index conversation messages into conv_vec for semantic search."""
        try:
            count = await self._embedding_store.index_conversation_messages(conv_id, agent_name)
            if count:
                log.debug("Indexed %d conversation chunks for %s", count, agent_name)
        except Exception:
            log.debug("Conversation indexing failed for %s", agent_name, exc_info=True)

    def _write_shared_event(
        self, agent_name: str, event_type: str, prompt_summary: str, result_summary: str,
    ) -> None:
        """Append a one-line event to shared/events.md so all agents see cross-team activity.

        Uses a sync lock via _schedule_event_write to avoid file corruption
        from concurrent writes. Fire-and-forget — callers don't await.
        """
        asyncio.create_task(self._write_shared_event_locked(
            agent_name, event_type, prompt_summary, result_summary,
        ))

    async def _write_shared_event_locked(
        self, agent_name: str, event_type: str, prompt_summary: str, result_summary: str,
    ) -> None:
        async with self._events_lock:
            try:
                data_dir = self.pm.config.data_dir
                events_file = data_dir / "shared" / "events.md"
                events_file.parent.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
                summary = f"{prompt_summary.strip()} → {result_summary.strip()}"
                entry = f"- [{ts}] **{agent_name}** ({event_type}): {summary[:200]}"
                lines: list[str] = []
                if events_file.exists():
                    lines = events_file.read_text().split("\n")
                lines.append(entry)
                if len(lines) > 100:
                    lines = lines[-100:]
                events_file.write_text("# Agent Events\n\n" + "\n".join(lines) + "\n")
            except Exception:
                log.debug("Failed to write shared event for %s", agent_name)

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
            try:
                team_signals = self.store.get_summaries_by_type("light_sleep", limit=5)
                if team_signals:
                    signal_text = "\n".join(s[:200] for s in team_signals)
                    signals_block = f"## Recent Team Memory Signals\n{signal_text}"
                    dynamic_context = f"{dynamic_context}\n\n{signals_block}" if dynamic_context else signals_block
            except Exception:
                pass
            try:
                active_plans = self.store.get_active_plans(agent_name=agent.name)
                if active_plans:
                    import json as _json
                    plan_lines = []
                    for p in active_plans[:3]:
                        try:
                            steps = _json.loads(p.get("steps_json", "[]"))
                        except (ValueError, TypeError):
                            steps = []
                        step_summary = ", ".join(
                            f"{s.get('step', s.get('description', '?')[:40])}" +
                            (" ✓" if s.get("done") else "")
                            for s in (steps if isinstance(steps, list) else [])
                        )
                        plan_lines.append(f"- **{p['goal'][:80]}**: {step_summary}")
                    plans_block = f"## Active Plans\n" + "\n".join(plan_lines)
                    dynamic_context = f"{dynamic_context}\n\n{plans_block}" if dynamic_context else plans_block
            except Exception:
                pass
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

        # Post-response: shared events, memory signals, conversation indexing, self-reflection
        if not response.is_error and response.output_tokens >= 50:
            self._write_shared_event(agent.name, "conversation", prompt[:100], response.result[:150])
            if self._compactor:
                asyncio.create_task(self._extract_signals(conv["session_id"], agent.name))
            if self._embedding_store and self._embedding_store.available:
                asyncio.create_task(self._index_conversation(conv["id"], agent.name))
            asyncio.create_task(self._self_reflect(agent, prompt, response.result, conv["id"]))

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
        prior_results: dict[str, str] = {}
        for target_name, flag, message in delegations:
            target = self.registry.get(target_name.lower())
            if not target:
                appended.append(f"\n[Delegation to '{target_name}' failed: agent not found]")
                continue

            task_type = _resolve_task_type(flag)
            log.info(
                "Delegation: %s -> %s (flag=%s task_type=%s)",
                from_agent.name, target_name, flag or "none", task_type,
            )
            self.store.record_audit(
                action="agent_delegation", agent_name=from_agent.name,
                details=(
                    f"delegated to {target_name} (flag={flag or 'none'}, "
                    f"task_type={task_type}): {message.strip()[:200]}"
                ),
            )

            effective_message = message.strip()
            if prior_results:
                chain_context = "\n".join(
                    f"[Result from {name}]:\n{res[:800]}"
                    for name, res in prior_results.items()
                )
                effective_message = f"{chain_context}\n\n{effective_message}"

            try:
                result = await self.agent_to_agent(
                    from_agent, target, effective_message,
                    task_type=task_type,
                )

                msg_lower = effective_message.lower()
                has_criteria = (
                    "acceptance criteria" in msg_lower
                    or "acceptance_criteria" in msg_lower
                )
                if has_criteria and platform not in ("delegation", "intercom", "verification"):
                    verify_resp = await self.send_to_agent(
                        agent=from_agent,
                        prompt=(
                            f"You delegated this task to {target_name}:\n{effective_message[:500]}\n\n"
                            f"Their response:\n{result[:1500]}\n\n"
                            f"The delegation included acceptance criteria. "
                            f"Check EACH criterion is met. "
                            f"Is this complete and correct? Are there tests if code was changed? "
                            f"Does it meet the acceptance criteria? "
                            f"Reply VERIFIED if ALL criteria met, or describe what's missing."
                        ),
                        platform="verification",
                        user_id=f"verify:{from_agent.name}",
                    )
                    if "VERIFIED" not in verify_resp.result.upper()[:200]:
                        log.info("Delegation %s->%s: not verified, re-delegating", from_agent.name, target_name)
                        result = await self.agent_to_agent(
                            from_agent, target,
                            f"Your previous response was incomplete. Feedback from {from_agent.name}:\n"
                            f"{verify_resp.result[:500]}\n\nOriginal task: {message.strip()}",
                        )
                elif len(result) > 200 and platform not in ("delegation", "intercom", "verification"):
                    # No acceptance criteria — inject a one-per-session tip so the agent
                    # learns to include criteria next time (prevents tip spam).
                    if from_agent.name not in self._delegate_tip_sent:
                        self._delegate_tip_sent.add(from_agent.name)
                        result += (
                            "\n\n[Tip: next time include `Acceptance criteria:` in your "
                            "[DELEGATE] tag for automatic verification]"
                        )

                prior_results[target_name] = result
                appended.append(
                    f"\n\n--- Response from {target.identity.display_name} ---\n{result}"
                )
                self._audit_delegation(
                    from_agent=from_agent,
                    target=target,
                    prompt=message.strip(),
                    result=result,
                    task_type=task_type,
                )
            except Exception as exc:
                detail = str(exc)[:200]
                log.exception("Delegation from %s to %s failed", from_agent.name, target_name)
                appended.append(f"\n[Delegation to '{target_name}' failed: {detail}]")

        if appended:
            response.result += "\n".join(appended)

        # Auto-create a plan when multi-step delegation is detected
        if len(delegations) >= 2:
            try:
                import json as _json
                steps = [
                    {"step": f"Delegate to {name}: {msg[:60]}", "agent": name, "done": name in prior_results}
                    for name, _flag, msg in delegations
                ]
                plan_id = uuid.uuid4().hex[:12]
                self.store.create_plan(
                    plan_id=plan_id,
                    agent_name=from_agent.name,
                    goal=f"Multi-step: {response.result[:80]}",
                    steps_json=_json.dumps(steps),
                )
                log.info("Auto-created plan %s with %d steps for %s", plan_id[:8], len(steps), from_agent.name)
            except Exception:
                pass

        # STATUS queries are always allowed — pure data, no LLM cost, no recursion risk
        response = await self._process_status_queries(from_agent, response)

        # Recursion guard: "intercom" = deepest level (no further nesting).
        # "delegation" = one level deep (can use help/discuss but those use intercom).
        # "discussion"/"council" = same: no further nesting.
        if platform not in ("discussion", "council", "intercom"):
            response = await self._process_help_requests(from_agent, response)
            if self._discussion_engine:
                response = await self._process_discussions(from_agent, response)
                if platform != "delegation":
                    response = await self._process_councils(from_agent, response)
            if self._workflow_engine:
                response = await self._process_optimizations(from_agent, response)
            if self._factory and platform != "delegation":
                response = await self._process_factory_requests(
                    from_agent, response, platform=platform,
                )

        response = await self._process_spawn_tags(from_agent, response)
        response = self._process_self_task_tags(from_agent, response)

        return response

    def _audit_delegation(
        self,
        from_agent: Agent,
        target: Agent,
        prompt: str,
        result: str,
        task_type: str,
    ) -> dict | None:
        """Emit a post-hoc rubric-compliance audit for a completed delegation.

        Parses the delegate's wrap-up for files_changed / loc_delta /
        new_tests_added, compares actual work-size against the model-routing
        rubric (shared/playbooks/model-routing.md), logs an INFO line, and
        persists the outcome to ``delegation_audit``. Always safe to call —
        any failure is swallowed so the delegation path can't break.
        """
        try:
            files_changed, loc_delta, new_tests = _parse_delegation_result(result)
            model_used = target.get_model(task_type)
            audit = compute_delegation_audit(
                files_changed=files_changed,
                loc_delta=loc_delta,
                new_test_files=new_tests,
                task_type=task_type,
                model_used=model_used,
                prompt=prompt,
            )
            task_id = uuid.uuid4().hex[:12]

            base = (
                f"model_choice={audit['outcome']} agent={target.name} "
                f"task={task_id} tripped={audit['tripped']} "
                f"used={audit['used']} expected={audit['expected']}"
            )
            if audit["outcome"] == "under-routed":
                log.info("%s — cost waste", base)
            elif audit["outcome"] == "over-routed":
                log.info("%s — quality risk, bump to complex next time", base)
            else:
                log.info(base)

            # Persist — swallow errors so an audit-table issue never breaks
            # the main delegation flow. The log line above is still emitted.
            try:
                self.store.record_delegation_audit(
                    task_id=task_id,
                    agent_name=target.name,
                    task_type_used=task_type,
                    model_used=model_used,
                    tripped_count=audit["tripped"],
                    outcome=audit["outcome"],
                    files_changed=files_changed,
                    loc_delta=loc_delta,
                    new_test_files=new_tests,
                    prompt_sample=prompt[:200],
                )
            except Exception:
                log.debug("delegation_audit persistence failed", exc_info=True)

            return {**audit, "task_id": task_id, "model_used": model_used}
        except Exception:
            log.debug("delegation audit failed for %s", target.name, exc_info=True)
            return None

    async def _process_spawn_tags(
        self, from_agent: Agent, response: ClaudeResponse,
    ) -> ClaudeResponse:
        """Process [SPAWN:agent] tags — fire-and-forget background tasks."""
        scan_text = _strip_code_blocks(response.result)
        spawns = SPAWN_PATTERN.findall(scan_text)
        if not spawns:
            return response

        appended = []
        for target_name, prompt_text in spawns:
            target = self.registry.get(target_name.lower())
            if not target:
                appended.append(f"\n[Spawn to '{target_name}' failed: agent not found]")
                continue
            prompt_text = prompt_text.strip()
            if not prompt_text:
                continue
            log.info("Spawn: %s -> %s: %s", from_agent.name, target_name, prompt_text[:80])
            self.store.record_audit(
                action="agent_spawn", agent_name=from_agent.name,
                details=f"spawned task on {target_name}: {prompt_text[:200]}",
            )
            try:
                task = self.spawn_task(target, prompt_text, platform="spawn")
                appended.append(f"\n[Spawned background task {task.task_id[:8]} on {target.identity.display_name}]")
                self._write_shared_event(
                    from_agent.name, "spawn",
                    f"spawned task on {target_name}", prompt_text[:150],
                )
            except Exception:
                log.exception("Spawn from %s to %s failed", from_agent.name, target_name)
                appended.append(f"\n[Spawn to '{target_name}' failed: error]")

        if appended:
            response.result += "\n".join(appended)
        return response

    def _process_self_task_tags(
        self, from_agent: Agent, response: ClaudeResponse,
    ) -> ClaudeResponse:
        """Process [TASK:self] tags — add to the agent's own pending task queue."""
        scan_text = _strip_code_blocks(response.result)
        tasks = SELF_TASK_PATTERN.findall(scan_text)
        if not tasks:
            return response

        appended = []
        for prompt_text in tasks:
            prompt_text = prompt_text.strip()
            if not prompt_text:
                continue
            task_id = uuid.uuid4().hex[:12]
            log.info("Self-task: %s queued: %s", from_agent.name, prompt_text[:80])
            self.store.create_task(
                task_id, from_agent.name, prompt_text,
                task_type="self", platform="self", user_id="self",
                source="agent", initial_status="pending",
            )
            appended.append(f"\n[Queued self-task {task_id[:8]}: {prompt_text[:60]}]")
            self._write_shared_event(
                from_agent.name, "self_task", "queued self-task", prompt_text[:150],
            )

        if appended:
            response.result += "\n".join(appended)
        return response

    async def _process_help_requests(
        self, from_agent: Agent, response: ClaudeResponse,
    ) -> ClaudeResponse:
        """Process [HELP:name] tags — quick single-turn consultation."""
        helps = HELP_PATTERN.findall(_strip_code_blocks(response.result))
        if not helps:
            return response

        appended = []
        for target_name, flag, question in helps:
            target = self.registry.get(target_name.lower())
            if not target:
                appended.append(f"\n[Help from '{target_name}' failed: agent not found]")
                continue

            task_type = _resolve_task_type(flag)
            log.info(
                "Help request: %s -> %s (flag=%s task_type=%s)",
                from_agent.name, target_name, flag or "none", task_type,
            )
            self.store.record_audit(
                action="agent_help_request", agent_name=from_agent.name,
                details=(
                    f"help from {target_name} (flag={flag or 'none'}, "
                    f"task_type={task_type}): {question.strip()[:200]}"
                ),
            )
            try:
                result = await self.agent_to_agent(
                    from_agent, target,
                    f"[Help request from {from_agent.name}]\n\n{question.strip()}",
                    task_type=task_type,
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
                try:
                    team_signals = self.store.get_summaries_by_type("light_sleep", limit=5)
                    if team_signals:
                        signal_text = "\n".join(s[:200] for s in team_signals)
                        signals_block = f"## Recent Team Memory Signals\n{signal_text}"
                        dynamic_context = f"{dynamic_context}\n\n{signals_block}" if dynamic_context else signals_block
                except Exception:
                    pass
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
            # flows through. A clean end_turn or tool_use with empty accumulated
            # text is legitimate and shouldn't spam as a warning; only genuine
            # infra errors or unexpected empty non-end stops do.
            stop_reason = getattr(resp, "stop_reason", "") or ""
            # tool_use: model's last action was a tool call — any text yielded
            # before it is valid output. Promote to non-error if we have text.
            if stop_reason == "tool_use" and resp.is_error and accumulated:
                resp.is_error = False
                resp.result = resp.result or accumulated
            empty_without_error = (
                not resp.is_error and not accumulated
                and stop_reason not in {"end_turn", "tool_use", ""}
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

        # Process delegation/spawn/self-task tags from the streamed response.
        # Yield progress strings so the user sees delegation activity in real-time.
        if not resp.is_error and (accumulated or resp.result):
            full_text = accumulated or resp.result
            scan_text = _strip_code_blocks(full_text)

            delegations = DELEGATION_PATTERN.findall(scan_text)
            prior_results: dict[str, str] = {}
            for target_name, flag, message_text in delegations:
                target = self.registry.get(target_name.lower())
                if not target:
                    yield f"\n[Delegation to '{target_name}' failed: agent not found]\n"
                    continue
                del_task_type = _resolve_task_type(flag)
                yield f"\n[→ Delegating to {target.identity.display_name}...]\n"
                effective_msg = message_text.strip()
                if prior_results:
                    chain_ctx = "\n".join(
                        f"[Result from {n}]:\n{r[:800]}" for n, r in prior_results.items()
                    )
                    effective_msg = f"{chain_ctx}\n\n{effective_msg}"
                try:
                    del_result = await self.agent_to_agent(
                        agent, target, effective_msg,
                        task_type=del_task_type,
                    )
                    prior_results[target_name] = del_result
                    yield f"\n[← {target.identity.display_name}: {del_result[:300]}]\n"
                    accumulated += f"\n\n--- Response from {target.identity.display_name} ---\n{del_result}"
                    self._audit_delegation(
                        from_agent=agent,
                        target=target,
                        prompt=message_text.strip(),
                        result=del_result,
                        task_type=del_task_type,
                    )
                except Exception as exc:
                    yield f"\n[Delegation to '{target_name}' failed: {str(exc)[:150]}]\n"

            spawns = SPAWN_PATTERN.findall(scan_text)
            for target_name, prompt_text in spawns:
                target = self.registry.get(target_name.lower())
                if target and prompt_text.strip():
                    try:
                        task = self.spawn_task(target, prompt_text.strip(), platform="spawn")
                        yield f"\n[Spawned background task {task.task_id[:8]} on {target.identity.display_name}]\n"
                        self._write_shared_event(agent.name, "spawn", f"spawned on {target_name}", prompt_text[:150])
                    except Exception:
                        pass

            self_tasks = SELF_TASK_PATTERN.findall(scan_text)
            for task_text in self_tasks:
                task_text = task_text.strip()
                if task_text:
                    tid = uuid.uuid4().hex[:12]
                    self.store.create_task(
                        tid, agent.name, task_text,
                        task_type="self", platform="self", user_id="self",
                        source="agent", initial_status="pending",
                    )
                    yield f"\n[Queued self-task: {task_text[:60]}]\n"

        # Post-response: shared events, memory signals, conversation indexing, self-reflection
        if not resp.is_error and resp.output_tokens >= 50:
            self._write_shared_event(agent.name, "conversation", prompt[:100], (accumulated or resp.result)[:150])
            if self._compactor:
                asyncio.create_task(self._extract_signals(conv["session_id"], agent.name))
            if self._embedding_store and self._embedding_store.available:
                asyncio.create_task(self._index_conversation(conv["id"], agent.name))
            asyncio.create_task(self._self_reflect(agent, prompt, (accumulated or resp.result), conv["id"]))

        yield resp

    async def agent_to_agent(
        self,
        from_agent: Agent,
        to_agent: Agent,
        message: str,
        task_type: str = "default",
    ) -> str:
        """Enable one agent to send a message to another.

        The receiving agent sees the message prefixed with the sender's name
        plus recent conversation context so it can act with full awareness.
        Platform is "delegation" (allows one level of nesting — the delegate
        can use [HELP]/[DELEGATE] but those calls use "intercom" to prevent
        infinite recursion).
        """
        conv_context = ""
        try:
            from_conv = self.store.get_or_create_conversation(
                session_id=None, platform="delegation",
                user_id=f"agent:{from_agent.name}",
            )
            recent = self.store.get_conversation_text(from_conv["id"], limit=10)
            if recent:
                conv_context = (
                    f"\n\n## Context from {from_agent.name}'s recent conversation\n"
                    f"{recent[-2000:]}"
                )
        except Exception:
            pass

        prompt = (
            f"[Task from {from_agent.name} — complete fully and report back]"
            f"{conv_context}\n\n{message}"
        )
        response = await self.send_to_agent(
            agent=to_agent,
            prompt=prompt,
            platform="delegation",
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
                if response.is_error:
                    spawned.status = "failed"
                else:
                    result_lower = (response.result or "").lower()
                    has_evidence = any(marker in result_lower for marker in (
                        "test", "verified", "confirmed", "passed", "checked",
                        "result:", "output:", "completed:", "done:",
                    ))
                    if has_evidence or len(response.result or "") < 100:
                        spawned.status = "completed"
                    else:
                        spawned.status = "completed_unverified"
                        log.warning(
                            "Task %s on %s: completed but result lacks verification evidence",
                            task_id[:8], agent.name,
                        )
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
            self._write_shared_event(
                agent.name, f"task_{spawned.status}",
                prompt[:80], (spawned.result or "")[:150],
            )

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
