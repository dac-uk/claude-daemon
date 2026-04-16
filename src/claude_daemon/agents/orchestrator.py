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

# Pattern to detect delegation requests in agent responses: [DELEGATE:agent_name] message
DELEGATION_PATTERN = re.compile(
    r'\[DELEGATE:(\w+)\]\s*(.*?)(?=\[DELEGATE:|\[DISCUSS:|\[COUNCIL\]|\[HELP:|\[OPTIMIZE:|\Z)',
    re.DOTALL,
)

# [DISCUSS:agent_name] topic — request a bilateral discussion
DISCUSS_PATTERN = re.compile(
    r'\[DISCUSS:(\w+)\]\s*(.*?)(?=\[DISCUSS:|\[COUNCIL\]|\[DELEGATE:|\[HELP:|\[OPTIMIZE:|\Z)',
    re.DOTALL,
)

# [COUNCIL] topic — request a full council deliberation
COUNCIL_PATTERN = re.compile(
    r'\[COUNCIL\]\s*(.*?)(?=\[DISCUSS:|\[COUNCIL\]|\[DELEGATE:|\[HELP:|\[OPTIMIZE:|\Z)',
    re.DOTALL,
)

# [HELP:agent_name] question — quick single-turn consultation
HELP_PATTERN = re.compile(
    r'\[HELP:(\w+)\]\s*(.*?)(?=\[HELP:|\[DISCUSS:|\[COUNCIL\]|\[DELEGATE:|\[OPTIMIZE:|\Z)',
    re.DOTALL,
)

# [OPTIMIZE:agent_name] target — trigger evo code optimization workflow
OPTIMIZE_PATTERN = re.compile(
    r'\[OPTIMIZE:(\w+)\]\s*(.*?)(?=\[OPTIMIZE:|\[DELEGATE:|\[DISCUSS:|\[COUNCIL\]|\[HELP:|\Z)',
    re.DOTALL,
)

_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```', re.DOTALL)


def _strip_code_blocks(text: str) -> str:
    """Remove fenced code blocks to prevent false tag matches in examples."""
    return _CODE_BLOCK_RE.sub('', text)


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
        self._spawned_tasks: dict[str, SpawnedTask] = {}

    def set_discussion_engine(self, engine) -> None:
        """Inject discussion engine (avoids circular init)."""
        self._discussion_engine = engine

    def set_workflow_engine(self, engine) -> None:
        """Inject workflow engine (avoids circular init)."""
        self._workflow_engine = engine

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

    async def auto_route(self, message: str) -> Agent:
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

        # Semantic memory search for task-relevant context
        semantic_matches = await self._semantic_search(prompt)

        agent_context = agent.build_system_context(semantic_matches=semantic_matches)
        agent_context += f"\n\n{self.registry.get_agent_summary()}"

        conv = self.store.get_or_create_conversation(
            session_id=session_id,
            platform=platform,
            user_id=f"{user_id}:{agent.name}",
        )

        self.store.add_message(conv["id"], "user", prompt)

        # Use agent's model for this task type
        model = agent.get_model(task_type)

        if self.hub:
            await self.hub.agent_busy(agent.name, prompt)

        # Use lite MCP config for chat (fewer servers = faster startup)
        mcp_path = (
            agent.mcp_lite_config_path
            if task_type in ("chat", "default")
            else agent.mcp_config_path
        )

        # Use pre-warmed session if available (MCP servers already initialized)
        session = self.pm.get_prewarmed_session(agent.name) or conv["session_id"]

        response = await self.pm.send_message(
            prompt=prompt,
            session_id=session,
            system_context=agent_context,
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

        # Process delegation tags in response (skip discussion tags when inside a discussion)
        if not response.is_error:
            response = await self._process_delegations(agent, response, platform=platform)

        # Pre-warm next session in background (MCP servers already initialized when needed)
        if not response.is_error and task_type in ("chat", "default"):
            asyncio.create_task(
                self.pm.prewarm_session(
                    agent_name=agent.name,
                    mcp_config_path=mcp_path,
                    settings_path=agent.settings_path,
                    model_override=model,
                )
            )

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
        if not delegations:
            return response

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

        # Skip discussion/help/council/optimize tags inside discussion turns (prevent recursion)
        if platform not in ("discussion", "council", "intercom"):
            response = await self._process_help_requests(from_agent, response)
            if self._discussion_engine:
                response = await self._process_discussions(from_agent, response)
                response = await self._process_councils(from_agent, response)
            if self._workflow_engine:
                response = await self._process_optimizations(from_agent, response)

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
                appended.append(
                    f"\n\n--- Council Decision ---\n"
                    f"Outcome: {result.outcome} | "
                    f"Cost: ${result.total_cost:.4f} | "
                    f"Participants: {', '.join(result.config.participants)}\n\n"
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
        semantic_matches = await self._semantic_search(prompt)

        agent_context = agent.build_system_context(semantic_matches=semantic_matches)
        agent_context += f"\n\n{self.registry.get_agent_summary()}"

        conv = self.store.get_or_create_conversation(
            session_id=session_id,
            platform=platform,
            user_id=f"{user_id}:{agent.name}",
        )

        self.store.add_message(conv["id"], "user", prompt)

        accumulated = ""
        final_response = None

        model = agent.get_model(task_type)

        if self.hub:
            await self.hub.agent_busy(agent.name, prompt)

        # Use lite MCP config for chat (fewer servers = faster startup)
        mcp_path = (
            agent.mcp_lite_config_path
            if task_type in ("chat", "default")
            else agent.mcp_config_path
        )

        async for chunk in self.pm.stream_message(
            prompt=prompt,
            session_id=conv["session_id"],
            system_context=agent_context,
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
    ) -> SpawnedTask:
        """Launch a task on an agent in the background (non-blocking).

        Each spawned task gets its own fresh session so multiple tasks
        to the same agent run truly in parallel (no --resume collision).
        Returns immediately with a SpawnedTask for tracking.
        """
        # Cleanup old completed tasks to prevent memory leak
        self._cleanup_finished_tasks()

        task_id = str(uuid.uuid4())[:12]

        # Persist to SQLite so tasks survive daemon restarts
        try:
            self.store.create_task(
                task_id, agent.name, prompt[:2000],
                task_type=task_type, platform=platform, user_id=user_id,
            )
        except Exception:
            log.debug("Could not persist task %s to DB", task_id)

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
            # Update DB with final status
            try:
                self.store.update_task_status(
                    task_id, spawned.status,
                    result=spawned.result[:5000] if spawned.result else None,
                    error=spawned.result if spawned.status == "failed" else None,
                    cost_usd=spawned.cost,
                )
            except Exception:
                pass
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
        import time
        now = time.monotonic()
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
