"""EvolutionActuator — closes the self-improvement loop.

The ImprovementPlanner generates improvement plans but never applies them.
This module turns those plans into concrete SOUL.md and AGENTS.md mutations,
with safety guards to prevent catastrophic data loss.

Safety model:
- Archive-before-write (timestamped backup of every mutated file)
- Size guard: reject if new content < 30% of old (data loss detection)
- Critical section preservation: ## Identity, ## Values, and ## Operating Directive never removed from SOUL.md
- Dry-run mode: proposals logged but not applied (default)
- All mutations recorded to evolution_log table and shared/evolution-log.md
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.core.process import ProcessManager
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)

EVOLUTION_PROPOSAL_PROMPT = """\
You are generating targeted improvements to AI agent system prompts based on
a weekly improvement plan. Each agent has SOUL.md (personality, values) and
AGENTS.md (operating rules, domain boundaries, planning protocol).

## Improvement Plan
{plan_text}

## Agent: {agent_name} ({agent_role})

### Current SOUL.md
{current_soul}

### Current AGENTS.md
{current_agents}

### Recent REFLECTIONS.md
{current_reflections}

## Your Task
Propose 0-3 minimal, targeted mutations to this agent's SOUL.md or AGENTS.md
that would address issues identified in the improvement plan.

Rules:
- NEVER remove or modify ## Identity, ## Values, or ## Operating Directive sections in SOUL.md
- Prefer APPENDING new sections over rewriting existing ones
- Each change should be small and focused (1-5 lines)
- Only propose changes that are clearly supported by the improvement plan
- If no changes are needed for this agent, return an empty array

Respond with ONLY a JSON array (no markdown):
[
  {{
    "file": "SOUL.md" or "AGENTS.md",
    "operation": "append_section" or "replace_section",
    "section_heading": "## Section Name",
    "new_content": "The new content for this section",
    "rationale": "One sentence explaining why"
  }}
]

If no changes needed, respond with: []
"""

# These sections in SOUL.md cannot be removed or replaced
PROTECTED_SECTIONS = {"## Identity", "## Values", "## Operating Directive"}


class EvolutionActuator:
    """Applies improvement proposals to agent identity files with safety guards."""

    def __init__(
        self,
        registry: AgentRegistry,
        pm: ProcessManager,
        store: ConversationStore,
        config: DaemonConfig,
        shared_dir: Path,
    ) -> None:
        self.registry = registry
        self.pm = pm
        self.store = store
        self.config = config
        self.shared_dir = shared_dir
        self._evolution_log = shared_dir / "evolution-log.md"
        self._archive_dir = shared_dir / "evolution-archive"

    async def run(self, plan_text: str) -> str:
        """Generate and apply evolution proposals for all agents.

        Returns a summary of what was proposed/applied.
        """
        if not self.config.evolution_enabled:
            return "Evolution disabled in config."

        self._archive_dir.mkdir(parents=True, exist_ok=True)
        if not self._evolution_log.exists():
            self._evolution_log.write_text(
                "# Evolution Log\n\nAuto-generated record of self-applied prompt mutations.\n\n"
            )

        dry_run = self.config.evolution_dry_run
        total_proposed = 0
        total_applied = 0
        summaries = []

        for agent in self.registry:
            try:
                proposals = await self._generate_proposals(agent, plan_text)
                if not proposals:
                    continue

                total_proposed += len(proposals)
                applied = self._apply_proposals(agent, proposals, dry_run=dry_run)
                total_applied += len(applied)

                for p in applied:
                    summaries.append(
                        f"{'[DRY RUN] ' if dry_run else ''}"
                        f"{agent.name}: {p['operation']} {p['section_heading']} "
                        f"in {p['file']} — {p['rationale']}"
                    )
            except Exception:
                log.exception("Evolution failed for agent %s", agent.name)

        mode = "DRY RUN" if dry_run else "APPLIED"
        summary = (
            f"Evolution cycle [{mode}]: {total_proposed} proposals, "
            f"{total_applied} {'logged' if dry_run else 'applied'} "
            f"across {len(self.registry)} agents."
        )
        if summaries:
            summary += "\n" + "\n".join(f"  - {s}" for s in summaries)

        log.info(summary)
        return summary

    async def _generate_proposals(self, agent, plan_text: str) -> list[dict]:
        """Ask Claude to generate evolution proposals for one agent."""
        soul_path = agent.workspace / "SOUL.md"
        agents_path = agent.workspace / "AGENTS.md"
        refl_path = agent.workspace / "REFLECTIONS.md"

        current_soul = soul_path.read_text()[:2000] if soul_path.exists() else ""
        current_agents = agents_path.read_text()[:2000] if agents_path.exists() else ""
        current_reflections = refl_path.read_text()[:1000] if refl_path.exists() else ""

        prompt = EVOLUTION_PROPOSAL_PROMPT.format(
            plan_text=plan_text[:3000],
            agent_name=agent.name,
            agent_role=agent.identity.role,
            current_soul=current_soul or "(empty)",
            current_agents=current_agents or "(empty)",
            current_reflections=current_reflections or "(none yet)",
        )

        response = await self.pm.send_message(
            prompt=prompt,
            system_context="You are a system prompt evolution engine. Respond with JSON only.",
            model_override="sonnet",
            max_budget=0.05,
            task_type="improvement",
        )

        if response.is_error:
            return []

        try:
            result = response.result.strip()
            # Handle markdown code blocks
            if result.startswith("```"):
                result = re.sub(r"^```\w*\n?", "", result)
                result = re.sub(r"\n?```$", "", result)
            proposals = json.loads(result)
            if not isinstance(proposals, list):
                return []
            return proposals
        except (json.JSONDecodeError, ValueError):
            log.debug("Could not parse evolution proposals for %s", agent.name)
            return []

    def _apply_proposals(
        self, agent, proposals: list[dict], dry_run: bool = True,
    ) -> list[dict]:
        """Validate and apply proposals. Returns list of applied proposals."""
        applied = []

        for proposal in proposals:
            file_name = proposal.get("file", "")
            if file_name not in ("SOUL.md", "AGENTS.md"):
                continue

            operation = proposal.get("operation", "")
            if operation not in ("append_section", "replace_section"):
                continue

            section = proposal.get("section_heading", "")
            new_content = proposal.get("new_content", "")
            rationale = proposal.get("rationale", "")

            if not section or not new_content:
                continue

            # Protect critical sections in SOUL.md (normalize heading format)
            if file_name == "SOUL.md" and operation == "replace_section":
                normalized = "## " + section.lstrip("#").strip()
                if any(normalized.lower().startswith(ps.lower()) for ps in PROTECTED_SECTIONS):
                    log.warning("Rejected: cannot replace protected section %s in SOUL.md", section)
                    continue

            file_path = agent.workspace / file_name
            if not file_path.exists():
                continue

            old_content = file_path.read_text()
            new_file_content = self._apply_operation(old_content, operation, section, new_content)

            # Size guard: reject if new content is suspiciously small
            if len(new_file_content) < len(old_content) * 0.3:
                log.warning(
                    "Rejected evolution for %s/%s: new (%d chars) < 30%% of old (%d chars)",
                    agent.name, file_name, len(new_file_content), len(old_content),
                )
                continue

            old_hash = hashlib.sha256(old_content.encode()).hexdigest()[:12]
            new_hash = hashlib.sha256(new_file_content.encode()).hexdigest()[:12]

            if old_hash == new_hash:
                continue  # No actual change

            # Record to DB
            try:
                self.store.record_evolution(
                    agent_name=agent.name,
                    file_changed=file_name,
                    operation=operation,
                    section_heading=section,
                    rationale=rationale,
                    old_content_hash=old_hash,
                    new_content_hash=new_hash,
                    dry_run=dry_run,
                )
            except Exception:
                pass

            # Log to shared evolution log
            self._log_evolution(agent.name, file_name, operation, section, rationale, dry_run)

            if not dry_run:
                # Archive before writing — abort if archive fails
                try:
                    from claude_daemon.memory.durable import DurableMemory
                    DurableMemory(agent.workspace / "memory").archive_file(
                        file_path, self._archive_dir,
                        prefix=f"{agent.name}_{file_name.replace('.md', '')}",
                    )
                except Exception:
                    log.error(
                        "Evolution archive failed for %s/%s — aborting write to prevent data loss",
                        agent.name, file_name,
                    )
                    continue
                file_path.write_text(new_file_content)
                agent.load_identity()
                log.info("Applied evolution to %s/%s: %s", agent.name, file_name, section)

            applied.append(proposal)

        return applied

    def _apply_operation(
        self, content: str, operation: str, section: str, new_text: str,
    ) -> str:
        """Apply a section operation to markdown content."""
        if operation == "append_section":
            # Add new section at end
            return content.rstrip() + f"\n\n{section}\n{new_text}\n"

        elif operation == "replace_section":
            # Find and replace the section (heading to next heading or EOF)
            pattern = re.compile(
                rf"^({re.escape(section)})\n.*?(?=^## |\Z)",
                re.MULTILINE | re.DOTALL,
            )
            if pattern.search(content):
                return pattern.sub(f"{section}\n{new_text}\n", content)
            else:
                # Section doesn't exist — append instead
                return content.rstrip() + f"\n\n{section}\n{new_text}\n"

        return content

    def _log_evolution(
        self, agent_name: str, file_name: str, operation: str,
        section: str, rationale: str, dry_run: bool,
    ) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        prefix = "[DRY RUN] " if dry_run else ""
        entry = (
            f"- [{ts}] {prefix}**{agent_name}** {operation} `{section}` in {file_name}: "
            f"{rationale}\n"
        )
        with open(self._evolution_log, "a") as f:
            f.write(entry)
