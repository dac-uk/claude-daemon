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
    r'\[DELEGATE:(\w+)\]\s*(.*?)(?=\[DELEGATE:|\Z)', re.DOTALL
)

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
    ) -> None:
        self.registry = registry
        self.pm = process_manager
        self.store = store
        self.hub = hub
        self._spawned_tasks: dict[str, SpawnedTask] = {}

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

        agent_context = agent.build_system_context()
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

        response = await self.pm.send_message(
            prompt=prompt,
            session_id=conv["session_id"],
            system_context=agent_context,
            platform=platform,
            user_id=user_id,
            model_override=model,
            mcp_config_path=agent.mcp_config_path,
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

        # Process delegation tags in response
        if not response.is_error:
            response = await self._process_delegations(agent, response)

        return response

    async def _process_delegations(
        self, from_agent: Agent, response: ClaudeResponse,
    ) -> ClaudeResponse:
        """Scan agent response for [DELEGATE:name] tags and execute inter-agent calls.

        Appends delegation results to the response text.
        """
        delegations = DELEGATION_PATTERN.findall(response.result)
        if not delegations:
            return response

        appended = []
        for target_name, message in delegations:
            target = self.registry.get(target_name.lower())
            if not target:
                appended.append(f"\n[Delegation to '{target_name}' failed: agent not found]")
                continue

            log.info("Delegation: %s -> %s", from_agent.name, target_name)
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
        agent_context = agent.build_system_context()
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

        async for chunk in self.pm.stream_message(
            prompt=prompt,
            session_id=conv["session_id"],
            system_context=agent_context,
            platform=platform,
            user_id=user_id,
            model_override=model,
            mcp_config_path=agent.mcp_config_path,
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
        task_id = str(uuid.uuid4())[:12]

        async def _run():
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
            if self.hub:
                await self.hub.task_update(
                    task_id, agent.name, spawned.status,
                    result=response.result, cost=response.cost,
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
