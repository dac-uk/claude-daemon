"""AgentRegistry - discovers, loads, and manages named agents.

Agents are stored as directories under the configured agents_dir:
  ~/.config/claude-daemon/agents/
  ├── orchestrator/   (SOUL.md, IDENTITY.md, ...)
  ├── coder/
  └── researcher/

The registry creates a default orchestrator agent if none exists.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

import yaml

from claude_daemon.agents.agent import Agent, AgentIdentity

log = logging.getLogger(__name__)


class AgentRegistry:
    """Manages the collection of named agents."""

    def __init__(self, agents_dir: Path, shared_dir: Path | None = None) -> None:
        self.agents_dir = agents_dir
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.shared_dir = shared_dir
        self._agents: dict[str, Agent] = {}

    def load_all(self) -> None:
        """Discover and load all agents from the agents directory."""
        self._agents.clear()

        for path in sorted(self.agents_dir.iterdir()):
            if path.is_dir() and not path.name.startswith("."):
                agent = self._load_agent(path)
                if agent:
                    self._agents[agent.name] = agent
                    log.info(
                        "Loaded agent: %s (role=%s, orchestrator=%s)",
                        agent.name, agent.identity.role, agent.is_orchestrator,
                    )

        if not self._agents:
            log.info("No agents found, creating default orchestrator")
            self._create_default_orchestrator()

    def _load_agent(self, workspace: Path) -> Agent | None:
        """Load an agent from a workspace directory."""
        name = workspace.name

        # Check for orchestrator marker
        is_orchestrator = False
        id_path = workspace / "IDENTITY.md"
        if id_path.exists():
            content = id_path.read_text().lower()
            is_orchestrator = "orchestrator" in content

        # Also check AGENTS.md for orchestrator flag
        agents_path = workspace / "AGENTS.md"
        if agents_path.exists():
            content = agents_path.read_text().lower()
            if "orchestrator: true" in content or "role: orchestrator" in content:
                is_orchestrator = True

        agent = Agent(
            name=name,
            workspace=workspace,
            is_orchestrator=is_orchestrator,
            shared_dir=self.shared_dir,
        )
        return agent

    def _create_default_orchestrator(self) -> None:
        """Create a default orchestrator agent."""
        workspace = self.agents_dir / "orchestrator"
        workspace.mkdir(parents=True, exist_ok=True)

        # SOUL.md
        (workspace / "SOUL.md").write_text(
            "# Soul\n\n"
            "I am the Orchestrator, the central coordinator for a team of AI agents.\n\n"
            "## Identity\n"
            "I route incoming requests to the best-suited specialist agent.\n"
            "When no specialist fits, I handle the request myself.\n"
            "I maintain awareness of all agents' capabilities and current workload.\n\n"
            "## Communication Style\n"
            "- Clear and efficient when delegating\n"
            "- Warm and helpful when interacting directly with users\n"
            "- I always identify which agent handled a task\n\n"
            "## Values\n"
            "- Route to specialists when possible\n"
            "- Be transparent about who is handling what\n"
            "- Keep the user informed of progress\n"
        )

        # IDENTITY.md
        (workspace / "IDENTITY.md").write_text(
            "# Identity\n\n"
            "Name: Orchestrator\n"
            "Role: orchestrator\n"
            "Emoji: \n"
        )

        # AGENTS.md
        (workspace / "AGENTS.md").write_text(
            "# Operating Rules\n\n"
            "## Routing\n"
            "When a message arrives:\n"
            "1. Assess which agent is best suited\n"
            "2. If a specialist matches, delegate to them\n"
            "3. If no specialist fits, handle it yourself\n"
            "4. Always inform the user which agent is handling their request\n\n"
            "## Memory\n"
            "- Shared memory is available to all agents\n"
            "- Each agent maintains its own MEMORY.md\n"
            "- Daily logs are per-agent\n\n"
            "orchestrator: true\n"
        )

        (workspace / "MEMORY.md").write_text("# Orchestrator - Persistent Memory\n\n")

        agent = Agent(name="orchestrator", workspace=workspace, is_orchestrator=True)
        self._agents["orchestrator"] = agent
        log.info("Created default orchestrator agent")

    # -- Access --

    def get(self, name: str) -> Agent | None:
        return self._agents.get(name)

    def get_orchestrator(self) -> Agent | None:
        for agent in self._agents.values():
            if agent.is_orchestrator:
                return agent
        return None

    def list_agents(self) -> list[Agent]:
        return list(self._agents.values())

    def agent_names(self) -> list[str]:
        return list(self._agents.keys())

    def __iter__(self) -> Iterator[Agent]:
        return iter(self._agents.values())

    def __len__(self) -> int:
        return len(self._agents)

    # -- Management --

    def create_agent(
        self,
        name: str,
        role: str = "",
        emoji: str = "",
        soul: str = "",
        is_orchestrator: bool = False,
    ) -> Agent:
        """Create a new agent with workspace and default files."""
        workspace = self.agents_dir / name
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "memory").mkdir(exist_ok=True)

        agent = Agent(
            name=name,
            workspace=workspace,
            identity=AgentIdentity(name=name, role=role, emoji=emoji),
            is_orchestrator=is_orchestrator,
        )

        if soul:
            (workspace / "SOUL.md").write_text(soul)
        agent.ensure_defaults()
        agent.load_identity()

        self._agents[name] = agent
        log.info("Created agent: %s (role=%s)", name, role)
        return agent

    def remove_agent(self, name: str) -> bool:
        """Remove an agent from the registry (does not delete files)."""
        if name in self._agents:
            del self._agents[name]
            return True
        return False

    def get_agent_summary(self) -> str:
        """Return a formatted summary of all agents for context injection."""
        lines = ["Available agents:"]
        for agent in self._agents.values():
            orch = " [ORCHESTRATOR]" if agent.is_orchestrator else ""
            role = f" - {agent.identity.role}" if agent.identity.role else ""
            lines.append(f"- {agent.identity.display_name}{role}{orch}")
        return "\n".join(lines)
