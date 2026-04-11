"""Orchestrator - routes messages to named agents and manages delegation.

The orchestrator is itself an agent, but with special routing responsibilities.
When a message arrives, it either:
1. Routes to a specifically addressed agent (@agent_name or /agent_name)
2. Lets the orchestrator agent decide who should handle it
3. Falls through to the orchestrator for direct handling
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, AsyncIterator

from claude_daemon.agents.agent import Agent
from claude_daemon.core.process import ClaudeResponse

if TYPE_CHECKING:
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.core.process import ProcessManager
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)

# Pattern to detect agent addressing: @coder, /coder, or "Hey coder,"
AGENT_ADDRESS_PATTERN = re.compile(
    r'^(?:@|/)(\w+)\b\s*(.*)', re.DOTALL
)

ROUTING_PROMPT = """\
You are the orchestrator. A message has arrived that needs routing to the right agent.

{agent_summary}

User message: {message}

Which agent should handle this? Respond with ONLY the agent name (lowercase).
If you should handle it yourself, respond with: orchestrator
If no agent is a good fit, respond with: orchestrator
"""


class Orchestrator:
    """Routes messages to appropriate agents and manages inter-agent communication."""

    def __init__(
        self,
        registry: AgentRegistry,
        process_manager: ProcessManager,
        store: ConversationStore,
    ) -> None:
        self.registry = registry
        self.pm = process_manager
        self.store = store

    def resolve_agent(self, message: str) -> tuple[Agent | None, str]:
        """Determine which agent should handle a message.

        Returns (agent, cleaned_message).
        If the message explicitly addresses an agent (@name or /name), route directly.
        Otherwise returns None (caller should use auto-routing).
        """
        match = AGENT_ADDRESS_PATTERN.match(message.strip())
        if match:
            agent_name = match.group(1).lower()
            remaining = match.group(2).strip()
            agent = self.registry.get(agent_name)
            if agent:
                return agent, remaining or message
            # Fall through if agent not found

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

        response = await self.pm.send_message(
            prompt=prompt,
            session_id=conv["session_id"],
            system_context=agent_context,
            platform=platform,
            user_id=user_id,
            model_override=model,
        )

        self.store.add_message(
            conv["id"], "assistant", response.result,
            tokens=response.output_tokens, cost=response.cost,
        )
        self.store.update_conversation(
            conv["id"], session_id=response.session_id, cost=response.cost,
        )

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

        async for chunk in self.pm.stream_message(
            prompt=prompt,
            session_id=conv["session_id"],
            system_context=agent_context,
            platform=platform,
            user_id=user_id,
            model_override=model,
        ):
            if isinstance(chunk, str):
                accumulated += chunk
                yield chunk
            elif isinstance(chunk, ClaudeResponse):
                final_response = chunk

        resp = final_response or ClaudeResponse.error("No response")
        self.store.add_message(
            conv["id"], "assistant", accumulated or resp.result,
            tokens=resp.output_tokens, cost=resp.cost,
        )
        self.store.update_conversation(
            conv["id"], session_id=resp.session_id, cost=resp.cost,
        )
        yield resp

    async def agent_to_agent(
        self,
        from_agent: Agent,
        to_agent: Agent,
        message: str,
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
        )
        return response.result
