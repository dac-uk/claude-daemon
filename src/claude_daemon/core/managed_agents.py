"""ManagedAgentBackend — execution backend using Anthropic's Managed Agents API.

Routes long-running, complex tasks (planning, workflows, memory consolidation)
through Anthropic's hosted agent infrastructure instead of local CLI subprocesses.

Requires: ANTHROPIC_API_KEY env var + anthropic SDK + managed_agents_enabled config.

All methods return the same ClaudeResponse type as the CLI backend so callers
are backend-agnostic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, AsyncIterator

from claude_daemon.core.process import ClaudeResponse

if TYPE_CHECKING:
    from claude_daemon.agents.agent import Agent
    from claude_daemon.core.config import DaemonConfig

log = logging.getLogger(__name__)

# Beta header required for Managed Agents API (April 2026)
_MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"


class ManagedAgentBackend:
    """Execution backend using Anthropic's Managed Agents API.

    Mirrors the interface of ProcessManager.send_message / stream_message
    so the dual-backend routing in ProcessManager can delegate transparently.
    """

    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self._agent_ids: dict[str, str] = {}  # daemon agent name → managed agent ID
        self._session_map: dict[str, str] = {}  # daemon session_id → managed session_id
        self._env_id: str | None = None
        self._client = None  # lazy-init

    def _ensure_client(self):
        """Lazy-initialize the Anthropic client."""
        if self._client is not None:
            return
        try:
            import anthropic
            self._client = anthropic.Anthropic(
                api_key=os.environ.get("ANTHROPIC_API_KEY"),
            )
        except ImportError:
            raise ImportError(
                "anthropic SDK is required for Managed Agents. "
                "Install with: pip install anthropic"
            )

    async def register_agent(self, agent: Agent) -> str:
        """Create or update a Managed Agent definition from a daemon agent.

        Maps the daemon agent's system prompt, model, and tools to
        a Managed Agent configuration on Anthropic's platform.
        Returns the managed agent ID (cached for session creation).
        """
        self._ensure_client()

        system_prompt = agent.build_system_context()
        model = agent.get_model("planning")  # Use the agent's best model

        try:
            result = await asyncio.to_thread(
                self._client.beta.managed_agents.agents.create,
                name=f"claude-daemon-{agent.name}",
                model=model or "claude-sonnet-4-5-20250514",
                instructions=system_prompt[:16000],  # API limit
                tools=self._build_tools(agent),
                beta=_MANAGED_AGENTS_BETA,
            )
            agent_id = result.id
            self._agent_ids[agent.name] = agent_id
            log.info("Registered managed agent: %s -> %s", agent.name, agent_id)
            return agent_id
        except Exception as e:
            log.warning("Failed to register managed agent %s: %s", agent.name, e)
            raise

    async def ensure_environment(self) -> str:
        """Create or reuse the cloud container environment.

        Configures a sandboxed environment with standard dev tools.
        Returns the environment ID.
        """
        if self._env_id:
            return self._env_id

        self._ensure_client()

        try:
            result = await asyncio.to_thread(
                self._client.beta.managed_agents.environments.create,
                beta=_MANAGED_AGENTS_BETA,
            )
            self._env_id = result.id
            log.info("Created managed agent environment: %s", self._env_id)
            return self._env_id
        except Exception as e:
            log.warning("Failed to create environment: %s", e)
            raise

    async def send_message(
        self,
        prompt: str,
        agent_name: str,
        session_id: str | None = None,
        system_context: str | None = None,
        max_budget: float = 0.50,
        platform: str = "managed",
        user_id: str = "local",
        model_override: str | None = None,
        settings_path: str | None = None,
        effort: str | None = None,
    ) -> ClaudeResponse:
        """Buffered execution via Managed Agents API.

        Creates a session, sends the prompt, waits for completion,
        and returns a ClaudeResponse matching the CLI backend's shape.
        """
        self._ensure_client()

        agent_id = self._agent_ids.get(agent_name)
        if not agent_id:
            return ClaudeResponse.error(
                f"Managed agent '{agent_name}' not registered. "
                "Call register_agent() first."
            )

        start = time.monotonic()
        try:
            # Reuse existing managed session for conversation continuity,
            # or create a new one if this is the first message
            managed_session_id = self._session_map.get(session_id) if session_id else None

            if not managed_session_id:
                session = await asyncio.to_thread(
                    self._client.beta.managed_agents.sessions.create,
                    agent_id=agent_id,
                    environment_id=self._env_id,
                    beta=_MANAGED_AGENTS_BETA,
                )
                managed_session_id = session.id
                if session_id:
                    self._session_map[session_id] = managed_session_id

            # Send user message
            await asyncio.to_thread(
                self._client.beta.managed_agents.sessions.events.create,
                session_id=managed_session_id,
                event={
                    "type": "user_message",
                    "content": prompt,
                },
                beta=_MANAGED_AGENTS_BETA,
            )

            # Poll for completion (SSE not easily available in sync client)
            result_text = ""
            total_input_tokens = 0
            total_output_tokens = 0

            # Stream events until session completes
            events = await asyncio.to_thread(
                self._client.beta.managed_agents.sessions.events.list,
                session_id=managed_session_id,
                beta=_MANAGED_AGENTS_BETA,
            )

            for event in events.data:
                if event.type == "assistant_message":
                    if hasattr(event, "content") and event.content:
                        if isinstance(event.content, str):
                            result_text = event.content
                        elif isinstance(event.content, list):
                            for block in event.content:
                                if hasattr(block, "text"):
                                    result_text += block.text
                if hasattr(event, "usage"):
                    total_input_tokens += getattr(event.usage, "input_tokens", 0)
                    total_output_tokens += getattr(event.usage, "output_tokens", 0)

            duration_ms = int((time.monotonic() - start) * 1000)

            # Estimate cost (rough pricing)
            cost = self._estimate_cost(
                total_input_tokens, total_output_tokens, model_override
            )

            response = ClaudeResponse(
                result=result_text or "(no response from managed agent)",
                session_id=managed_session_id,
                cost=cost,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                num_turns=1,
                duration_ms=duration_ms,
                is_error=not result_text,
                model_used=model_override or "",
            )

            log.info(
                "Managed agent response: agent=%s, session=%s, cost=$%.4f, duration=%dms",
                agent_name, managed_session_id[:12], cost, duration_ms,
            )
            return response

        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.error("Managed agent error for %s: %s", agent_name, e)
            return ClaudeResponse(
                result=f"Managed agent error: {e}",
                session_id="",
                cost=0, input_tokens=0, output_tokens=0,
                num_turns=0, duration_ms=duration_ms, is_error=True,
            )

    async def stream_message(
        self,
        prompt: str,
        agent_name: str,
        session_id: str | None = None,
        system_context: str | None = None,
        max_budget: float = 0.50,
        platform: str = "managed",
        user_id: str = "local",
        model_override: str | None = None,
        settings_path: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[str | ClaudeResponse]:
        """Streaming execution via Managed Agents API.

        Yields text deltas as they arrive, then a final ClaudeResponse.
        Falls back to buffered mode if streaming isn't available.
        """
        # For now, use buffered mode and yield the full response
        # TODO: Implement true SSE streaming when SDK support matures
        response = await self.send_message(
            prompt=prompt,
            agent_name=agent_name,
            session_id=session_id,
            system_context=system_context,
            max_budget=max_budget,
            platform=platform,
            user_id=user_id,
            model_override=model_override,
        )
        if response.result and not response.is_error:
            yield response.result
        yield response

    async def steer_session(self, session_id: str, message: str) -> None:
        """Send a mid-execution steering event to a running managed session.

        This is a new capability not possible with CLI subprocess —
        allows redirecting a running agent without killing and restarting.
        """
        self._ensure_client()
        try:
            await asyncio.to_thread(
                self._client.beta.managed_agents.sessions.events.create,
                session_id=session_id,
                event={
                    "type": "user_message",
                    "content": message,
                },
                beta=_MANAGED_AGENTS_BETA,
            )
            log.info("Steered session %s", session_id[:12])
        except Exception as e:
            log.warning("Failed to steer session %s: %s", session_id[:12], e)

    def get_status(self) -> dict:
        """Return status information about the managed agents backend."""
        return {
            "enabled": self.config.managed_agents_enabled,
            "environment_id": self._env_id,
            "registered_agents": list(self._agent_ids.keys()),
            "agent_count": len(self._agent_ids),
            "task_types": list(self.config.managed_agents_task_types),
            "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        }

    def _build_tools(self, agent: Agent) -> list[dict]:
        """Map daemon agent tools to Managed Agents tool definitions."""
        tools = [
            {"type": "bash"},
            {"type": "file_read"},
            {"type": "file_write"},
            {"type": "file_edit"},
            {"type": "web_search"},
            {"type": "web_fetch"},
        ]
        return tools

    @staticmethod
    def _estimate_cost(
        input_tokens: int, output_tokens: int, model: str | None
    ) -> float:
        """Rough cost estimation based on token counts and model."""
        model = (model or "sonnet").lower()
        if "opus" in model:
            return (input_tokens * 15 + output_tokens * 75) / 1_000_000
        elif "haiku" in model:
            return (input_tokens * 0.25 + output_tokens * 1.25) / 1_000_000
        else:  # sonnet
            return (input_tokens * 3 + output_tokens * 15) / 1_000_000
