"""ImprovementPlanner — the recursive self-improvement engine.

Runs periodically to:
1. Assess agent performance and team capability gaps
2. Aggregate reflections into shared learnings
3. Generate improvement proposals and initiative suggestions
4. Deliver proactive recommendations to the user
5. Write playbooks from accumulated experience

This is the nervous system that turns passive agents into a learning organisation.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.core.process import ProcessManager
    from claude_daemon.memory.store import ConversationStore

log = logging.getLogger(__name__)

IMPROVEMENT_PLAN_PROMPT = """\
You are the improvement planner for a team of AI agents. Your job is to turn
reflections and metrics into concrete improvement actions.

## Agent Reflections (from individual agents)
{agent_reflections}

## Team Metrics (last 7 days)
{metrics_summary}

## Current Playbooks (accumulated lessons)
{playbook_index}

## Previous Improvement Plan
{previous_plan}

## Your Task
Generate a prioritised improvement plan:

1. **What's working well** — 2-3 strengths to maintain
2. **What needs fixing** — 2-3 concrete problems with root causes
3. **Capability gaps** — skills or tools the team is missing
4. **Proposed initiatives** — 3-5 specific, actionable improvement proposals:
   - Each should have: title, owner (agent name), expected outcome, effort estimate
   - Prioritise by ROI (impact / effort)
5. **Suggestions for Dave** — 1-3 ideas that need human input or approval:
   - New integrations, business ideas, strategic pivots, investment decisions

Be specific and actionable. No vague advice. Each proposal should be something
an agent can start working on in their next heartbeat cycle.
"""

LEARNING_SYNTHESIS_PROMPT = """\
You are synthesising learnings from a team of AI agents into shared knowledge.

## Individual Agent Reflections
{agent_reflections}

## Recent Events
{recent_events}

## Your Task
Write a concise "Team Learnings" document:
- What patterns are emerging across agents?
- What mistakes keep recurring?
- What techniques have proven effective?
- What should all agents know going forward?

Keep it under 500 words. Focus on actionable, cross-cutting insights.
"""

SELF_ASSESSMENT_PROMPT = """\
You are {agent_name} ({agent_role}). Conduct an honest self-assessment.

## Your Recent Work
{recent_work}

## Your Current Memory
{current_memory}

## Your Reflections So Far
{current_reflections}

## Task
Write an updated REFLECTIONS.md for yourself:

1. **Performance rating** (1-5): How well did I execute this period?
2. **What I did well** — specific examples
3. **What I struggled with** — honest assessment
4. **Skills to develop** — what would make me more effective?
5. **Tools I need** — any capabilities I'm missing?
6. **Process improvements** — changes to how I work that would help
7. **Ideas for the team** — suggestions that would benefit everyone

Be specific. Name files, tasks, and outcomes. This reflection feeds into
the team improvement cycle.
"""


class ImprovementPlanner:
    """Drives the recursive self-improvement loop."""

    def __init__(
        self,
        registry: AgentRegistry,
        pm: ProcessManager,
        store: ConversationStore,
        shared_dir: Path,
    ) -> None:
        self.registry = registry
        self.pm = pm
        self.store = store
        self.shared_dir = shared_dir

    async def run_weekly_improvement_cycle(self) -> str:
        """The main improvement loop. Run weekly after REM sleep.

        1. Collect per-agent reflections
        2. Synthesise shared learnings
        3. Generate improvement plan
        4. Write steering directives for agents
        5. Return summary for delivery to user
        """
        log.info("Starting weekly improvement cycle")

        # Step 1: Collect all agent reflections
        agent_reflections = self._collect_agent_reflections()

        # Step 2: Synthesise into shared learnings
        await self._synthesise_learnings(agent_reflections)

        # Step 3: Generate improvement plan
        plan = await self._generate_improvement_plan(agent_reflections)

        # Step 4: Write the plan
        plan_path = self.shared_dir / "playbooks" / "improvement-plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)

        # Archive previous plan
        if plan_path.exists():
            archive_dir = self.shared_dir / "playbooks" / "archive"
            archive_dir.mkdir(exist_ok=True)
            ts = date.today().isoformat()
            (archive_dir / f"improvement-plan-{ts}.md").write_text(
                plan_path.read_text()
            )

        plan_path.write_text(f"# Improvement Plan — {date.today().isoformat()}\n\n{plan}")

        log.info("Improvement cycle complete. Plan written to %s", plan_path)
        return plan

    async def run_agent_self_assessment(self, agent) -> None:
        """Run self-assessment for a single agent, updating their REFLECTIONS.md."""
        # Get recent conversations for this agent
        convos = self.store.get_active_conversations()
        agent_convos = [
            c for c in convos
            if c.get("user_id", "").endswith(f":{agent.name}")
        ]

        recent_work = []
        for conv in agent_convos[:5]:
            text = self.store.get_conversation_text(conv["id"], limit=10)
            if text:
                recent_work.append(text[:500])

        if not recent_work:
            return

        memory = ""
        mem_path = agent.workspace / "MEMORY.md"
        if mem_path.exists():
            memory = mem_path.read_text()[:1500]

        reflections = ""
        refl_path = agent.workspace / "REFLECTIONS.md"
        if refl_path.exists():
            reflections = refl_path.read_text()[:1000]

        prompt = SELF_ASSESSMENT_PROMPT.format(
            agent_name=agent.name,
            agent_role=agent.identity.role,
            recent_work="\n---\n".join(recent_work),
            current_memory=memory or "(no memory yet)",
            current_reflections=reflections or "(first self-assessment)",
        )

        response = await self.pm.send_message(
            prompt=prompt,
            max_budget=0.08,
            platform="system",
            user_id=f"self-assess:{agent.name}",
            model_override=agent.get_model("scheduled"),
            task_type="improvement",
        )

        if response.is_error or len(response.result) < 30:
            log.warning("Self-assessment for %s failed", agent.name)
            return

        refl_path.write_text(response.result)
        log.info("Updated REFLECTIONS.md for %s (%d chars)", agent.name, len(response.result))

    async def run_all_self_assessments(self) -> None:
        """Run self-assessments for all agents."""
        for agent in self.registry:
            try:
                await self.run_agent_self_assessment(agent)
            except Exception:
                log.exception("Self-assessment failed for %s", agent.name)

    def _collect_agent_reflections(self) -> str:
        """Gather all agents' REFLECTIONS.md content."""
        parts = []
        for agent in self.registry:
            refl_path = agent.workspace / "REFLECTIONS.md"
            if refl_path.exists():
                content = refl_path.read_text().strip()
                if content:
                    parts.append(f"### {agent.identity.display_name} ({agent.identity.role})\n{content[:600]}")
        return "\n\n".join(parts) if parts else "(No agent reflections yet)"

    async def _synthesise_learnings(self, agent_reflections: str) -> None:
        """Synthesise cross-agent learnings into shared/learnings.md."""
        events = ""
        events_path = self.shared_dir / "events.md"
        if events_path.exists():
            events = events_path.read_text()[-2000:]

        prompt = LEARNING_SYNTHESIS_PROMPT.format(
            agent_reflections=agent_reflections,
            recent_events=events or "(no recent events)",
        )

        response = await self.pm.send_message(
            prompt=prompt,
            max_budget=0.06,
            platform="system",
            user_id="learnings-synthesis",
            model_override="haiku",
            task_type="improvement",
        )

        if response.is_error or len(response.result) < 30:
            log.warning("Learning synthesis produced insufficient output")
            return

        learnings_path = self.shared_dir / "learnings.md"
        learnings_path.write_text(
            f"# Team Learnings — {date.today().isoformat()}\n\n{response.result}"
        )
        log.info("Updated shared/learnings.md (%d chars)", len(response.result))

    async def _generate_improvement_plan(self, agent_reflections: str) -> str:
        """Generate the weekly improvement plan."""
        # Gather metrics
        metrics = self.store.get_agent_metrics(days=7)
        metrics_lines = []
        for m in metrics:
            metrics_lines.append(
                f"- {m.get('agent_name', '?')}: "
                f"{m.get('count', 0)} calls, "
                f"${m.get('total_cost', 0):.4f} cost, "
                f"{m.get('total_input', 0) + m.get('total_output', 0)} tokens"
            )
        metrics_summary = "\n".join(metrics_lines) if metrics_lines else "(no metrics yet)"

        # Playbook index
        playbooks_dir = self.shared_dir / "playbooks"
        playbook_index = []
        if playbooks_dir.is_dir():
            for pb in sorted(playbooks_dir.glob("*.md")):
                if pb.name != "improvement-plan.md":
                    playbook_index.append(f"- {pb.stem}")
        playbook_str = "\n".join(playbook_index) if playbook_index else "(no playbooks yet)"

        # Previous plan
        plan_path = playbooks_dir / "improvement-plan.md"
        previous = plan_path.read_text()[:1500] if plan_path.exists() else "(first improvement plan)"

        prompt = IMPROVEMENT_PLAN_PROMPT.format(
            agent_reflections=agent_reflections,
            metrics_summary=metrics_summary,
            playbook_index=playbook_str,
            previous_plan=previous,
        )

        response = await self.pm.send_message(
            prompt=prompt,
            max_budget=0.15,
            platform="system",
            user_id="improvement-planner",
            model_override="sonnet",
            task_type="improvement",
        )

        if response.is_error:
            log.error("Improvement plan generation failed: %s", response.result[:200])
            return "(Improvement plan generation failed)"

        return response.result
