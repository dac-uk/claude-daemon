"""Agent - represents a single named agent with its own identity and workspace.

Each agent has its own set of OpenClaw-style identity files:
- SOUL.md: Core personality, values, beliefs, tone
- IDENTITY.md: Public-facing name, role, emoji
- AGENTS.md: Operating procedures, workflow rules
- USER.md: Context about the user(s) this agent serves
- TOOLS.md: Capabilities and tool guidance
- MEMORY.md: Curated long-term knowledge
- VISION.md: Long-term goals and roadmap
- HEARTBEAT.md: Autonomous recurring tasks
- REFLECTIONS.md: Self-improvement learnings
- memory/YYYY-MM-DD.md: Daily activity logs
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class AgentIdentity:
    """Parsed identity from an agent's workspace files."""

    name: str
    role: str = ""
    emoji: str = ""
    soul: str = ""
    agents_rules: str = ""
    user_context: str = ""
    tools_guidance: str = ""
    vision: str = ""
    heartbeat_tasks: str = ""

    @property
    def display_name(self) -> str:
        prefix = f"{self.emoji} " if self.emoji else ""
        return f"{prefix}{self.name}"


@dataclass
class Agent:
    """A named agent with its own workspace, identity, and session state."""

    name: str
    workspace: Path
    identity: AgentIdentity = field(default=None)
    is_orchestrator: bool = False

    def __post_init__(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "memory").mkdir(exist_ok=True)
        if self.identity is None:
            self.identity = AgentIdentity(name=self.name)
        self.load_identity()

    def load_identity(self) -> None:
        """Load identity files from workspace into the identity object."""
        self.identity.name = self.name

        # SOUL.md
        soul_path = self.workspace / "SOUL.md"
        if soul_path.exists():
            self.identity.soul = soul_path.read_text()

        # IDENTITY.md
        id_path = self.workspace / "IDENTITY.md"
        if id_path.exists():
            content = id_path.read_text()
            self.identity.soul = self.identity.soul  # soul is separate
            for line in content.split("\n"):
                line = line.strip()
                if line.lower().startswith("role:"):
                    self.identity.role = line.split(":", 1)[1].strip()
                elif line.lower().startswith("emoji:"):
                    self.identity.emoji = line.split(":", 1)[1].strip()

        # AGENTS.md
        agents_path = self.workspace / "AGENTS.md"
        if agents_path.exists():
            self.identity.agents_rules = agents_path.read_text()

        # USER.md
        user_path = self.workspace / "USER.md"
        if user_path.exists():
            self.identity.user_context = user_path.read_text()

        # TOOLS.md
        tools_path = self.workspace / "TOOLS.md"
        if tools_path.exists():
            self.identity.tools_guidance = tools_path.read_text()

        # VISION.md
        vision_path = self.workspace / "VISION.md"
        if vision_path.exists():
            self.identity.vision = vision_path.read_text()

        # HEARTBEAT.md
        hb_path = self.workspace / "HEARTBEAT.md"
        if hb_path.exists():
            self.identity.heartbeat_tasks = hb_path.read_text()

    def build_system_context(self, max_chars: int = 6000) -> str:
        """Build the full system prompt context for this agent.

        Mirrors the OpenClaw boot sequence: SOUL -> IDENTITY -> AGENTS ->
        USER -> TOOLS -> MEMORY -> REFLECTIONS -> recent logs.
        """
        blocks = []
        ident = self.identity

        if ident.soul:
            blocks.append(ident.soul[:1200])

        if ident.role:
            blocks.append(f"Your name is {ident.name}. Role: {ident.role}")

        if ident.agents_rules:
            blocks.append(f"## Operating Rules\n{ident.agents_rules[:800]}")

        if ident.user_context:
            blocks.append(f"## User Context\n{ident.user_context[:600]}")

        if ident.tools_guidance:
            blocks.append(f"## Tools\n{ident.tools_guidance[:400]}")

        if ident.vision:
            blocks.append(f"## Vision\n{ident.vision[:400]}")

        # Memory
        memory_path = self.workspace / "MEMORY.md"
        if memory_path.exists():
            mem = memory_path.read_text()
            if mem:
                blocks.append(f"## Memory\n{mem[:1500]}")

        # Reflections
        refl_path = self.workspace / "REFLECTIONS.md"
        if refl_path.exists():
            refl = refl_path.read_text()
            if refl:
                blocks.append(f"## Self-Reflections\n{refl[:400]}")

        context = "\n\n".join(blocks)
        if len(context) > max_chars:
            context = context[:max_chars]

        return context

    def ensure_defaults(self) -> None:
        """Create default identity files if they don't exist."""
        soul_path = self.workspace / "SOUL.md"
        if not soul_path.exists():
            role_line = f" My role is: {self.identity.role}" if self.identity.role else ""
            soul_path.write_text(
                f"# Soul\n\nI am {self.name}, a persistent AI agent.{role_line}\n\n"
                f"## Values\n- Reliability and follow-through\n"
                f"- Clear, direct communication\n"
                f"- Proactive problem-solving\n"
            )

        id_path = self.workspace / "IDENTITY.md"
        if not id_path.exists():
            id_path.write_text(
                f"# Identity\n\n"
                f"Name: {self.name}\n"
                f"Role: {self.identity.role or 'General assistant'}\n"
                f"Emoji: {self.identity.emoji or ''}\n"
            )

        memory_path = self.workspace / "MEMORY.md"
        if not memory_path.exists():
            memory_path.write_text(f"# {self.name} - Persistent Memory\n\n")
