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
    gotchas: str = ""

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


# Default effort mapping — task type → reasoning depth
_EFFORT_BY_TASK_TYPE: dict[str, str] = {
    "scheduled": "low",
    "heartbeat": "low",
    "chat": "medium",
    "default": "medium",
    "planning": "high",
    "discussion": "medium",
}


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

        # GOTCHAS.md
        gotchas_path = self.workspace / "GOTCHAS.md"
        if gotchas_path.exists():
            self.identity.gotchas = gotchas_path.read_text()

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

    @property
    def mcp_lite_config_path(self) -> str | None:
        """Resolve path to this agent's lite MCP config (5 essential servers), or None."""
        path = self.workspace / "tools-lite.json"
        return str(path) if path.exists() else self.mcp_config_path

    @property
    def settings_path(self) -> str | None:
        """Resolve path to this agent's settings.json, or None."""
        path = self.workspace / "settings.json"
        return str(path) if path.exists() else None

    def get_effort(self, task_type: str = "default") -> str:
        """Get effort level for a task type.

        task_type: 'default', 'planning', 'chat', 'scheduled', 'heartbeat'
        """
        return _EFFORT_BY_TASK_TYPE.get(task_type, "medium")

    def check_mcp_health(self) -> dict[str, str]:
        """Check if agent's MCP tools config is valid. Returns {server_name: status}."""
        import json as _json
        result = {}
        path = self.mcp_config_path
        if not path:
            return result
        try:
            with open(path) as f:
                config = _json.load(f)
            servers = config.get("mcpServers", {})
            for name, server in servers.items():
                cmd = server.get("command", "")
                env = server.get("env", {})
                # Check for unresolved env var placeholders
                unresolved = [k for k, v in env.items() if v.startswith("${") and v.endswith("}")]
                if unresolved:
                    result[name] = f"unconfigured ({', '.join(unresolved)})"
                else:
                    result[name] = "configured"
        except Exception as e:
            result["_error"] = str(e)
        return result

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

    def build_system_context(self, max_chars: int = 8000, semantic_matches: list[dict] | None = None) -> str:
        """Build the full system prompt context for this agent.

        Priority-ordered: critical blocks are never truncated, lower-priority
        blocks are trimmed first if the total exceeds max_chars.

        Tiers:
          1. CRITICAL (never cut): SOUL, identity, steering, planning protocol
          2. HIGH: operating rules, user context, memory
          3. MEDIUM: reflections, tools, events, learnings
          4. LOW: playbook index, logging convention, vision
        """
        ident = self.identity

        # -- Tier 1: Critical (never truncated) --
        critical: list[str] = []

        if ident.soul:
            critical.append(ident.soul)

        # Team operating directive — read from shared/DIRECTIVE.md (Tier 1, never truncated)
        if self.shared_dir:
            directive_path = self.shared_dir / "DIRECTIVE.md"
            if directive_path.exists():
                directive = directive_path.read_text().strip()
                if directive:
                    critical.append(f"<important>\n{directive}\n</important>")

        if ident.role:
            critical.append(f"Your name is {ident.name}. Role: {ident.role}")

        # Steering — mid-task redirection from orchestrator (highest priority)
        if self.shared_dir:
            steer_path = self.shared_dir / "steer" / f"{self.name}.md"
            if steer_path.exists():
                steer = steer_path.read_text().strip()
                if steer:
                    critical.append(
                        f"<important>\n## STEERING (priority instructions)\n{steer}\n</important>"
                    )

        critical.append(
            "<important>\n"
            "## Planning Protocol\n"
            "For multi-step or complex tasks:\n"
            "1. RESEARCH — Gather context. Read relevant files, check current state.\n"
            "2. PLAN — Outline approach, steps, dependencies, and risks.\n"
            "3. PUBLISH the plan to the user immediately.\n"
            "4. EXECUTE autonomously — do NOT wait for approval.\n"
            "5. VERIFY — Before declaring done, verify your output works correctly.\n"
            "   Run tests, check builds, confirm the change does what was asked.\n"
            "6. REPORT — Summarise what was done, what was verified, any issues found.\n"
            "If the plan changes during execution, update the user.\n"
            "Skip planning for simple single-step queries.\n"
            "</important>"
        )

        # -- Tier 2: High priority --
        high: list[str] = []

        if ident.gotchas:
            high.append(f"<important>\n## Gotchas\n{ident.gotchas}\n</important>")

        if ident.agents_rules:
            high.append(f"## Operating Rules\n{ident.agents_rules}")

        if ident.user_context:
            high.append(f"## User Context\n{ident.user_context}")

        memory_path = self.workspace / "MEMORY.md"
        if memory_path.exists():
            mem = memory_path.read_text()
            if mem:
                high.append(f"## Memory\n{mem}")

        # Semantic memory matches (from vector search on user's message)
        if semantic_matches:
            relevant = "\n".join(
                f"- [{m.get('source', '?')}] {m['chunk'][:300]}"
                for m in semantic_matches[:3]
            )
            high.append(f"## Related Context (semantic search)\n{relevant}")

        # -- Tier 3: Medium priority --
        medium: list[str] = []

        refl_path = self.workspace / "REFLECTIONS.md"
        if refl_path.exists():
            refl = refl_path.read_text()
            if refl:
                medium.append(f"## Self-Reflections\n{refl}")

        if ident.tools_guidance:
            medium.append(f"## Tools\n{ident.tools_guidance}")

        if not self.is_orchestrator:
            medium.append(
                "## Available Capabilities\n"
                "- When refactoring, review for reuse, quality, and efficiency\n"
                "- For bulk operations, run the same command across multiple files\n"
                "- For debugging, methodically isolate: read error, check assumptions, targeted fix\n"
                "- Always verify changes: run tests, check builds, confirm behaviour matches intent"
            )
            medium.append(
                "## Inter-Agent Communication\n"
                "You can communicate with other agents using these tags in your response:\n\n"
                "**[DELEGATE:agent-name] message** — One-shot handoff. Use when: clear task for "
                "another agent, no discussion needed.\n\n"
                "**[HELP:agent-name] question** — Quick consultation. Use when: you have a specific "
                "question, need a fact-check or sanity check from another agent.\n\n"
                "**[DISCUSS:agent-name] topic** — Multi-turn bilateral discussion. Use when:\n"
                "- You need to align on an approach that spans your domains\n"
                "- You're uncertain about something in another agent's domain\n"
                "- A decision requires input from both sides before proceeding\n\n"
                "**[COUNCIL] topic** — Full council deliberation with all agents. Use when:\n"
                "- Decision affects multiple domains (architecture + design + cost)\n"
                "- High-stakes choice with significant consequences\n"
                "- You're stuck and need diverse perspectives\n\n"
                "**[STATUS:agent-name]** — Check an agent's health and activity (free, no LLM call).\n"
                "**[STATUS]** — Fleet-wide summary of all agents. Use before delegating to check availability.\n\n"
                "**Decision guide:** Simple task → DELEGATE | Quick question → HELP | "
                "Need alignment → DISCUSS | High-stakes/multi-domain → COUNCIL | "
                "Check availability → STATUS | Unsure? Start with HELP, escalate to DISCUSS if needed."
            )
        else:
            medium.append(
                "## Inter-Agent Communication\n"
                "As orchestrator, you have these communication powers:\n\n"
                "**[DELEGATE:agent-name] message** — Assign a task to a specific agent.\n\n"
                "**[HELP:agent-name] question** — Quick consultation with a specialist.\n\n"
                "**[DISCUSS:agent-name] topic** — Bilateral discussion with another agent.\n\n"
                "**[COUNCIL] topic** — Convene the full council for deliberation. Use for:\n"
                "- Strategic decisions affecting multiple domains\n"
                "- Disagreements that need resolution\n"
                "- High-stakes choices (before escalating to the user)\n"
                "- Architecture decisions, major refactors, new initiatives\n\n"
                "**[STATUS:agent-name]** — Check an agent's health and activity (free, no LLM call).\n"
                "**[STATUS]** — Fleet-wide summary of all agents.\n\n"
                "**Council Protocol:** State the topic clearly → each agent provides their "
                "domain perspective → you synthesize into a clear decision → only escalate "
                "to the user if: capital >500, legal exposure, public commitments, or genuine "
                "deadlock after council."
            )

        if self.shared_dir:
            events_path = self.shared_dir / "events.md"
            if events_path.exists():
                events = events_path.read_text()
                if events:
                    medium.append(f"## Recent Agent Activity\n{events[-500:]}")

            learnings_path = self.shared_dir / "learnings.md"
            if learnings_path.exists():
                learnings = learnings_path.read_text()
                if learnings:
                    medium.append(f"## Team Learnings\n{learnings[-600:]}")

        # -- Tier 4: Low priority --
        low: list[str] = []

        if ident.vision:
            low.append(f"## Vision\n{ident.vision}")

        if self.shared_dir:
            playbooks_dir = self.shared_dir / "playbooks"
            if playbooks_dir.is_dir():
                playbook_index = []
                for pb in sorted(playbooks_dir.glob("*.md"))[-10:]:
                    playbook_index.append(f"- {pb.stem}")
                if playbook_index:
                    low.append(
                        "## Shared Playbooks (team lessons)\n"
                        + "\n".join(playbook_index)
                        + "\nRead these when working on related tasks."
                    )

        low.append(
            "## Logging Convention\n"
            "Tag key reasoning: [THOUGHT] [ACTION] [OBSERVATION] [REASONING]\n"
            "Tag trimmable output: [TOOL-OUTPUT] [METADATA]\n"
            "5-10 tags per task. Enables memory reconstruction."
        )

        # Assemble with priority-based trimming
        critical_text = "\n\n".join(critical)
        remaining = max_chars - len(critical_text)

        def _fit(blocks: list[str], budget: int) -> tuple[str, int]:
            """Fit as many blocks as possible into budget, truncating the last."""
            parts = []
            left = budget
            for block in blocks:
                if left <= 0:
                    break
                if len(block) <= left:
                    parts.append(block)
                    left -= len(block) + 2  # account for \n\n joiner
                else:
                    parts.append(block[:left])
                    left = 0
            return "\n\n".join(parts), left

        high_text, remaining = _fit(high, remaining)
        medium_text, remaining = _fit(medium, remaining)
        low_text, _ = _fit(low, remaining)

        parts = [critical_text]
        if high_text:
            parts.append(high_text)
        if medium_text:
            parts.append(medium_text)
        if low_text:
            parts.append(low_text)

        return "\n\n".join(parts)

    def build_static_context(self, max_chars: int = 6000) -> str:
        """Build the static system context — set once at SDK session creation.

        Includes: soul, identity, rules, gotchas, communication tags, capabilities.
        Excludes: semantic matches, events, learnings (those are dynamic per message).
        """
        return self.build_system_context(max_chars=max_chars, semantic_matches=None)

    def build_dynamic_context(self, semantic_matches: list[dict] | None = None) -> str:
        """Build per-message dynamic context (injected in user message body).

        Includes: semantic matches, recent events, learnings.
        Returns empty string if nothing dynamic is available.
        """
        parts: list[str] = []

        if semantic_matches:
            relevant = "\n".join(
                f"- [{m.get('source', '?')}] {m['chunk'][:300]}"
                for m in semantic_matches[:3]
            )
            parts.append(f"## Related Context (semantic search)\n{relevant}")

        if self.shared_dir:
            events_path = self.shared_dir / "events.md"
            if events_path.exists():
                events = events_path.read_text()
                if events:
                    parts.append(f"## Recent Agent Activity\n{events[-500:]}")

            learnings_path = self.shared_dir / "learnings.md"
            if learnings_path.exists():
                learnings = learnings_path.read_text()
                if learnings:
                    parts.append(f"## Team Learnings\n{learnings[-600:]}")

        return "\n\n".join(parts)

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
