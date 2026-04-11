"""Agent - represents a single named agent with its own identity and workspace.

Each agent has its own set of identity files:
- SOUL.md: Core personality, values, beliefs, tone
- IDENTITY.md: Public-facing name, role, emoji, model config
- AGENTS.md: Operating procedures, workflow rules
- USER.md: Context about the user (or symlink to shared/USER.md)
- TOOLS.md: Capabilities and tool guidance
- MEMORY.md: Curated long-term knowledge
- VISION.md: Long-term goals and roadmap
- HEARTBEAT.md: Autonomous recurring tasks
- REFLECTIONS.md: Self-improvement learnings
- memory/YYYY-MM-DD.md: Daily activity logs
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class HeartbeatTask:
    """A single recurring task parsed from HEARTBEAT.md."""

    title: str
    cron: str
    model: str = "haiku"
    prompt: str = ""


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

    # Per-agent model routing
    default_model: str = "sonnet"
    planning_model: str = "opus"
    chat_model: str = "sonnet"
    scheduled_model: str = "haiku"

    # Per-agent MCP tool configuration
    mcp_config: str = ""  # Filename of MCP config JSON in workspace (e.g. "tools.json")

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
    shared_dir: Path | None = None  # Path to shared/ workspace

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

        # IDENTITY.md (includes model config)
        id_path = self.workspace / "IDENTITY.md"
        if id_path.exists():
            for line in id_path.read_text().split("\n"):
                line = line.strip()
                key_val = line.split(":", 1)
                if len(key_val) != 2:
                    continue
                key, val = key_val[0].strip().lower(), key_val[1].strip()
                if not val:
                    continue
                if key == "role":
                    self.identity.role = val
                elif key == "emoji":
                    self.identity.emoji = val
                elif key == "model":
                    self.identity.default_model = val
                elif key == "planning-model":
                    self.identity.planning_model = val
                elif key == "chat-model":
                    self.identity.chat_model = val
                elif key == "scheduled-model":
                    self.identity.scheduled_model = val
                elif key == "mcp-config":
                    self.identity.mcp_config = val

        # AGENTS.md
        agents_path = self.workspace / "AGENTS.md"
        if agents_path.exists():
            self.identity.agents_rules = agents_path.read_text()

        # USER.md (check agent workspace first, then shared)
        user_path = self.workspace / "USER.md"
        if user_path.exists():
            self.identity.user_context = user_path.read_text()
        elif self.shared_dir and (self.shared_dir / "USER.md").exists():
            self.identity.user_context = (self.shared_dir / "USER.md").read_text()

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

    @property
    def mcp_config_path(self) -> str | None:
        """Resolve the full path to this agent's MCP config JSON, or None."""
        if not self.identity.mcp_config:
            return None
        path = self.workspace / self.identity.mcp_config
        if path.exists():
            return str(path)
        return None

    def get_model(self, task_type: str = "default") -> str:
        """Get the appropriate model for a task type.

        task_type: 'default', 'planning', 'chat', 'scheduled'
        """
        models = {
            "default": self.identity.default_model,
            "planning": self.identity.planning_model,
            "chat": self.identity.chat_model,
            "scheduled": self.identity.scheduled_model,
        }
        return models.get(task_type, self.identity.default_model)

    def build_system_context(self, max_chars: int = 8000) -> str:
        """Build the full system prompt context for this agent.

        Boot sequence: SOUL -> IDENTITY -> AGENTS -> USER -> TOOLS ->
        MEMORY -> REFLECTIONS -> recent logs.
        """
        blocks = []
        ident = self.identity

        if ident.soul:
            blocks.append(ident.soul[:1500])

        if ident.role:
            blocks.append(f"Your name is {ident.name}. Role: {ident.role}")

        if ident.agents_rules:
            blocks.append(f"## Operating Rules\n{ident.agents_rules[:1000]}")

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

        # Shared workspace context
        if self.shared_dir:
            # Steering (mid-task redirection from orchestrator)
            steer_path = self.shared_dir / "steer" / f"{self.name}.md"
            if steer_path.exists():
                steer = steer_path.read_text().strip()
                if steer:
                    blocks.append(f"## STEERING (priority instructions)\n{steer[:500]}")

            # Shared event log for inter-agent awareness
            events_path = self.shared_dir / "events.md"
            if events_path.exists():
                events = events_path.read_text()
                if events:
                    blocks.append(f"## Recent Agent Activity\n{events[-500:]}")

            # Shared playbooks — accumulated lessons from all agents
            playbooks_dir = self.shared_dir / "playbooks"
            if playbooks_dir.is_dir():
                playbook_index = []
                for pb in sorted(playbooks_dir.glob("*.md"))[-10:]:
                    playbook_index.append(f"- {pb.stem}")
                if playbook_index:
                    blocks.append(
                        "## Shared Playbooks (team lessons)\n"
                        + "\n".join(playbook_index)
                        + "\nRead these when working on related tasks."
                    )

            # Shared learnings — cross-agent improvement insights
            learnings_path = self.shared_dir / "learnings.md"
            if learnings_path.exists():
                learnings = learnings_path.read_text()
                if learnings:
                    blocks.append(f"## Team Learnings\n{learnings[-600:]}")

        # Planning protocol
        blocks.append(
            "## Planning Protocol\n"
            "For multi-step or complex tasks: ALWAYS plan first using Opus-level reasoning.\n"
            "1. Outline your approach, steps, dependencies, and risks.\n"
            "2. Publish the plan to the user IMMEDIATELY.\n"
            "3. Execute autonomously — do NOT wait for approval.\n"
            "4. Update the user if the plan changes during execution.\n"
            "Skip planning for simple single-step queries."
        )

        # OTA logging convention
        blocks.append(
            "## Logging Convention\n"
            "Tag key reasoning: [THOUGHT] [ACTION] [OBSERVATION] [REASONING]\n"
            "Tag trimmable output: [TOOL-OUTPUT] [METADATA]\n"
            "5-10 tags per task. Enables memory reconstruction."
        )

        context = "\n\n".join(blocks)
        if len(context) > max_chars:
            context = context[:max_chars]

        return context

    def parse_heartbeat_tasks(self) -> list[HeartbeatTask]:
        """Parse HEARTBEAT.md into structured tasks.

        Format:
            ## Task Title
            Cron: 0 9 * * *
            Model: haiku
            The prompt text for this task (everything until the next ## heading).
        """
        hb_path = self.workspace / "HEARTBEAT.md"
        if not hb_path.exists():
            return []

        content = hb_path.read_text()
        tasks: list[HeartbeatTask] = []

        # Split on ## headings
        sections = re.split(r'^## ', content, flags=re.MULTILINE)
        for section in sections:
            section = section.strip()
            if not section:
                continue

            lines = section.split("\n")
            title = lines[0].strip()

            cron = ""
            model = "haiku"
            prompt_lines: list[str] = []

            for line in lines[1:]:
                stripped = line.strip()
                if stripped.lower().startswith("cron:"):
                    cron = stripped.split(":", 1)[1].strip()
                elif stripped.lower().startswith("model:"):
                    model = stripped.split(":", 1)[1].strip()
                elif stripped:
                    prompt_lines.append(stripped)

            if cron and prompt_lines:
                tasks.append(HeartbeatTask(
                    title=title,
                    cron=cron,
                    model=model,
                    prompt="\n".join(prompt_lines),
                ))

        return tasks

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
                f"Model: {self.identity.default_model}\n"
                f"Planning-Model: {self.identity.planning_model}\n"
                f"Chat-Model: {self.identity.chat_model}\n"
                f"Scheduled-Model: {self.identity.scheduled_model}\n"
            )

        memory_path = self.workspace / "MEMORY.md"
        if not memory_path.exists():
            memory_path.write_text(f"# {self.name} - Persistent Memory\n\n")
