"""Inter-agent discussion engine — bilateral dialogue and council deliberation.

Provides multi-turn conversation between agents, replacing the one-shot
delegation pattern with richer collaboration modes:

- **Bilateral**: Two agents discuss a topic, alternating turns until
  convergence, cost cap, or turn limit.
- **Council**: All agents deliberate; orchestrator synthesizes a decision.

Triggered by agents emitting [DISCUSS:name] or [COUNCIL] tags in responses,
processed by the orchestrator alongside existing [DELEGATE:name] tags.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_daemon.agents.orchestrator import Orchestrator
    from claude_daemon.agents.registry import AgentRegistry
    from claude_daemon.core.config import DaemonConfig
    from claude_daemon.memory.store import ConversationStore
    from claude_daemon.orchestration.task_api import TaskAPI

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

DISCUSSION_TURN_PROMPT = """\
You are in a {discussion_type} discussion.

## Topic
{topic}

{context_section}
## Discussion So Far
{transcript}

## Your Turn
Provide your perspective as {agent_name} ({agent_role}).
{turn_instructions}
If you believe the group has reached agreement, include the word CONSENSUS.
Keep your response focused and under 400 words.
"""

SYNTHESIS_PROMPT = """\
You are synthesizing a council discussion into a clear decision.

## Topic
{topic}

## Full Transcript
{transcript}

## Participants
{participants}

## Your Task
Produce in this exact order:

1. **Decision**: Clear conclusion (1-2 sentences)
2. **Rationale**: Key arguments that led here (3-5 bullets)
3. **Dissent**: Any unresolved disagreements (or "None.")
4. **Action Items**: Prose summary of who does what next.
5. **Executable Actions** (REQUIRED): A fenced ```json block matching \
the schema below. Each entry becomes a real task assigned to the named \
agent. Use ONLY agent names from the Participants list above. Omit any \
action that needs human judgement before it can safely execute — flag \
those in Dissent instead.

```json
{{"actions": [
  {{"owner": "<agent_name>",
    "prompt": "<single concrete instruction, imperative voice, max 400 chars>",
    "priority": "high|medium|low",
    "requires_approval": false}}
]}}
```

If there are genuinely no safe executable actions, emit `{{"actions": []}}`.
Be decisive. If no clear consensus, make the call based on weight of arguments.
"""


# ---------------------------------------------------------------------------
# Action-item extraction from synthesis
# ---------------------------------------------------------------------------

_ACTIONS_BLOCK = re.compile(
    r"```json\s*(\{[\s\S]*?\"actions\"[\s\S]*?\})\s*```", re.MULTILINE,
)


def _extract_action_items(synthesis_text: str) -> list[dict]:
    """Pull the fenced JSON action block out of a synthesis string.

    Returns [] on malformed/absent block (never raises).
    """
    m = _ACTIONS_BLOCK.search(synthesis_text or "")
    if not m:
        return []
    try:
        payload = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        log.warning("Council synthesis had malformed action JSON")
        return []
    actions = payload.get("actions") or []
    cleaned: list[dict] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        owner = str(a.get("owner", "")).strip()
        prompt = str(a.get("prompt", "")).strip()
        if not owner or not prompt:
            continue
        cleaned.append({
            "owner": owner,
            "prompt": prompt[:2000],
            "priority": a.get("priority", "medium"),
            "requires_approval": bool(a.get("requires_approval", False)),
        })
    return cleaned


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DiscussionConfig:
    """Configuration for a single discussion session."""

    topic: str
    initiator: str
    participants: list[str]
    discussion_type: str  # "bilateral" or "council"
    max_turns: int = 6
    max_cost: float = 1.00
    convergence_keyword: str = "CONSENSUS"
    context: str = ""
    task_type: str = "discussion"


@dataclass
class DiscussionTurn:
    """A single turn in a discussion."""

    agent_name: str
    content: str
    turn_number: int
    cost: float = 0.0
    duration_ms: int = 0
    is_error: bool = False


@dataclass
class DiscussionResult:
    """Complete result of a discussion session."""

    discussion_id: str
    config: DiscussionConfig
    turns: list[DiscussionTurn] = field(default_factory=list)
    synthesis: str = ""
    outcome: str = "completed"  # completed, converged, cost_exceeded, max_turns, error

    @property
    def total_cost(self) -> float:
        return sum(t.cost for t in self.turns)

    @property
    def total_duration_ms(self) -> int:
        return sum(t.duration_ms for t in self.turns)

    @property
    def transcript(self) -> str:
        parts = []
        for t in self.turns:
            parts.append(f"**{t.agent_name}** (turn {t.turn_number}):\n{t.content}")
        return "\n\n---\n\n".join(parts)

    def is_over_budget(self) -> bool:
        return self.total_cost > self.config.max_cost


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class DiscussionEngine:
    """Manages multi-turn discussions and council deliberations between agents."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        registry: AgentRegistry,
        store: ConversationStore,
        config: DaemonConfig | None = None,
        shared_dir: Path | None = None,
        hub=None,
        task_api: TaskAPI | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.registry = registry
        self.store = store
        self.config = config
        self.shared_dir = shared_dir
        self.hub = hub
        self.task_api = task_api

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def run_bilateral(
        self,
        agent_a: str,
        agent_b: str,
        topic: str,
        context: str = "",
        max_turns: int | None = None,
        max_cost: float | None = None,
        platform: str = "discussion",
        user_id: str = "discussion",
    ) -> DiscussionResult:
        """Two agents discuss a topic, alternating turns."""
        if agent_a.lower() == agent_b.lower():
            log.warning("Bilateral discussion rejected: agent_a == agent_b (%s)", agent_a)
            return DiscussionResult(
                discussion_id="rejected", outcome="error",
                config=DiscussionConfig(topic=topic, initiator=agent_a,
                                        participants=[agent_a], discussion_type="bilateral"),
            )
        effective_max_turns = max_turns or (self.config.discussion_max_turns if self.config else 6)
        effective_max_cost = max_cost if max_cost is not None else (
            self.config.discussion_max_cost if self.config else 1.00
        )

        cfg = DiscussionConfig(
            topic=topic,
            initiator=agent_a,
            participants=[agent_a, agent_b],
            discussion_type="bilateral",
            max_turns=effective_max_turns,
            max_cost=effective_max_cost,
            context=context,
        )
        return await self._run_discussion(cfg, platform, user_id)

    async def run_council(
        self,
        topic: str,
        participants: list[str] | None = None,
        context: str = "",
        max_rounds: int | None = None,
        max_cost: float | None = None,
        platform: str = "council",
        user_id: str = "council",
    ) -> DiscussionResult:
        """N-agent council deliberation with orchestrator synthesis."""
        if participants is None:
            participants = [a.name for a in self.registry.list_agents()]

        effective_max_rounds = max_rounds or (self.config.council_max_rounds if self.config else 2)
        effective_max_cost = max_cost if max_cost is not None else (
            self.config.council_max_cost if self.config else 2.00
        )
        # Total turns = participants * rounds
        total_turns = len(participants) * effective_max_rounds

        cfg = DiscussionConfig(
            topic=topic,
            initiator=participants[0] if participants else "council",
            participants=participants,
            discussion_type="council",
            max_turns=total_turns,
            max_cost=effective_max_cost,
            context=context,
        )

        result = await self._run_discussion(cfg, platform, user_id)

        # Synthesize via the orchestrator agent
        if result.turns and result.outcome != "error":
            result.synthesis = await self._synthesize(result, platform, user_id)

        # Spawn action tasks from the synthesis
        task_ids = await self._spawn_council_actions(result)

        # Update the DB record with synthesis + action task IDs
        # (_run_discussion recorded it before synthesis was available)
        self._update_discussion_post_synthesis(result, task_ids)

        return result

    # ------------------------------------------------------------------ #
    # Core discussion loop
    # ------------------------------------------------------------------ #

    async def _run_discussion(
        self,
        config: DiscussionConfig,
        platform: str,
        user_id: str,
    ) -> DiscussionResult:
        """Core loop: alternate turns, check cost/convergence, record."""
        discussion_id = str(uuid.uuid4())[:12]
        result = DiscussionResult(discussion_id=discussion_id, config=config)
        start_time = time.monotonic()

        log.info(
            "Discussion %s started: %s [%s] participants=%s",
            discussion_id, config.discussion_type, config.topic[:60],
            config.participants,
        )

        turn_number = 0
        try:
            for turn_number in range(1, config.max_turns + 1):
                # Determine whose turn it is
                agent_name = self._next_speaker(config, turn_number)
                agent = self.registry.get(agent_name.lower())
                if not agent:
                    log.warning("Discussion: agent '%s' not found, skipping", agent_name)
                    continue

                is_final = turn_number == config.max_turns
                prompt = self._build_turn_prompt(
                    agent_name, config, result.turns, is_final,
                )

                # Send to agent (NOT through _process_delegations to avoid recursion)
                response = await self.orchestrator.send_to_agent(
                    agent=agent,
                    prompt=prompt,
                    platform=platform,
                    user_id=f"{user_id}:disc:{discussion_id}",
                    task_type="discussion",
                )

                turn = DiscussionTurn(
                    agent_name=agent_name,
                    content=response.result if not response.is_error else f"[Error: {response.result[:200]}]",
                    turn_number=turn_number,
                    cost=response.cost,
                    duration_ms=response.duration_ms,
                    is_error=response.is_error,
                )
                result.turns.append(turn)

                # Check convergence
                if self._check_convergence(turn.content, config.convergence_keyword):
                    result.outcome = "converged"
                    log.info("Discussion %s converged at turn %d", discussion_id, turn_number)
                    break

                # Check cost cap
                if result.total_cost >= config.max_cost:
                    result.outcome = "cost_exceeded"
                    log.info("Discussion %s cost cap reached: $%.4f", discussion_id, result.total_cost)
                    break
            else:
                result.outcome = "max_turns"

        except Exception:
            log.exception("Discussion %s failed", discussion_id)
            result.outcome = "error"

        elapsed = int((time.monotonic() - start_time) * 1000)

        # Record to DB and markdown
        self._record_discussion(result, elapsed)

        log.info(
            "Discussion %s ended: outcome=%s turns=%d cost=$%.4f",
            discussion_id, result.outcome, len(result.turns), result.total_cost,
        )
        return result

    # ------------------------------------------------------------------ #
    # Turn management
    # ------------------------------------------------------------------ #

    def _next_speaker(self, config: DiscussionConfig, turn_number: int) -> str:
        """Determine which agent speaks at this turn number."""
        if config.discussion_type == "bilateral":
            # Alternate: A, B, A, B, ...
            idx = (turn_number - 1) % len(config.participants)
            return config.participants[idx]

        # Council: round-robin through all participants
        # Put non-orchestrator agents first, orchestrator last per round
        ordered = self._council_order(config)
        idx = (turn_number - 1) % len(ordered)
        return ordered[idx]

    def _council_order(self, config: DiscussionConfig) -> list[str]:
        """Order participants for council: non-orchestrator first, orchestrator last."""
        orchestrator = self.registry.get_orchestrator()
        orch_name = orchestrator.name if orchestrator else None
        non_orch = [p for p in config.participants if p != orch_name]
        if orch_name and orch_name in config.participants:
            non_orch.append(orch_name)
        return non_orch

    # ------------------------------------------------------------------ #
    # Prompt building
    # ------------------------------------------------------------------ #

    def _build_turn_prompt(
        self,
        agent_name: str,
        config: DiscussionConfig,
        turns: list[DiscussionTurn],
        is_final: bool = False,
    ) -> str:
        """Build the prompt for an agent's turn."""
        agent = self.registry.get(agent_name.lower())
        role = agent.identity.role if agent else "agent"

        # Format transcript
        if turns:
            transcript_parts = []
            for t in turns:
                transcript_parts.append(f"**{t.agent_name}**: {t.content}")
            transcript_text = "\n\n".join(transcript_parts)
        else:
            transcript_text = "(You are speaking first.)"

        # Turn instructions
        if is_final:
            instructions = (
                "This is the FINAL turn. Summarize the key points of agreement "
                "and any remaining disagreements. Try to reach a conclusion."
            )
        elif not turns:
            instructions = "You are opening the discussion. State your initial position clearly."
        else:
            instructions = (
                "Consider what has been said. Build on good ideas, challenge weak ones. "
                "Propose concrete next steps if appropriate."
            )

        context_section = f"## Context\n{config.context}\n\n" if config.context else ""

        return DISCUSSION_TURN_PROMPT.format(
            discussion_type=config.discussion_type,
            topic=config.topic,
            context_section=context_section,
            transcript=transcript_text,
            agent_name=agent_name,
            agent_role=role,
            turn_instructions=instructions,
        )

    # ------------------------------------------------------------------ #
    # Synthesis
    # ------------------------------------------------------------------ #

    async def _synthesize(
        self,
        result: DiscussionResult,
        platform: str,
        user_id: str,
    ) -> str:
        """Ask the orchestrator to synthesize the discussion into a decision."""
        orchestrator = self.registry.get_orchestrator()
        if not orchestrator:
            return ""

        prompt = SYNTHESIS_PROMPT.format(
            topic=result.config.topic,
            transcript=result.transcript,
            participants=", ".join(result.config.participants),
        )

        response = await self.orchestrator.send_to_agent(
            agent=orchestrator,
            prompt=prompt,
            platform=platform,
            user_id=f"{user_id}:synthesis:{result.discussion_id}",
            task_type="discussion",
        )

        if response.is_error:
            log.warning("Synthesis failed for discussion %s", result.discussion_id)
            return ""

        # Add synthesis cost to the result
        result.turns.append(DiscussionTurn(
            agent_name=orchestrator.name,
            content=response.result,
            turn_number=len(result.turns) + 1,
            cost=response.cost,
            duration_ms=response.duration_ms,
        ))

        return response.result

    # ------------------------------------------------------------------ #
    # Post-council action spawning
    # ------------------------------------------------------------------ #

    async def _spawn_council_actions(
        self, result: DiscussionResult,
    ) -> list[str]:
        """Extract action items from synthesis and submit each as a task.

        Returns list of created task_ids.
        """
        if not self.task_api:
            return []
        if self.config and not getattr(self.config, "council_auto_execute_actions", True):
            return []

        actions = _extract_action_items(result.synthesis)
        if not actions:
            return []

        max_n = getattr(self.config, "council_max_actions_per_run", 8) if self.config else 8
        actions = actions[:max_n]

        from claude_daemon.orchestration.task_api import TaskSubmission

        task_ids: list[str] = []
        for idx, a in enumerate(actions):
            if a["owner"] not in result.config.participants:
                log.warning(
                    "Council action owner %r not in participants; skipping",
                    a["owner"],
                )
                continue

            framed = (
                f"[From council {result.discussion_id}] "
                f"Topic: {result.config.topic}\n\n"
                f"Action ({a['priority']}): {a['prompt']}\n\n"
                f"Report back concretely on what you did, what you verified, "
                f"and what (if anything) still needs a human."
            )
            sub = TaskSubmission(
                prompt=framed,
                agent=a["owner"],
                source="council",
                task_type="council_action",
                metadata={
                    "discussion_id": result.discussion_id,
                    "topic": result.config.topic,
                    "priority": a["priority"],
                    "action_index": idx,
                },
                require_approval=a["requires_approval"],
                approval_reason=(
                    f"Council {result.discussion_id} action "
                    f"(owner={a['owner']})"
                ),
            )
            try:
                res = self.task_api.submit_task(sub)
                if res.task_id:
                    task_ids.append(res.task_id)
            except Exception:
                log.exception("Failed to spawn council action %d for %s", idx, a["owner"])

        if task_ids:
            log.info(
                "Council %s spawned %d action tasks: %s",
                result.discussion_id, len(task_ids), task_ids,
            )
        return task_ids

    # ------------------------------------------------------------------ #
    # Convergence detection
    # ------------------------------------------------------------------ #

    def _check_convergence(self, content: str, keyword: str) -> bool:
        """Check if agent response signals consensus."""
        return keyword.lower() in content.lower()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _record_discussion(
        self, result: DiscussionResult, duration_ms: int,
        action_task_ids: list[str] | None = None,
    ) -> None:
        """Persist discussion to DB and shared markdown file."""
        try:
            self.store.record_discussion(
                discussion_id=result.discussion_id,
                discussion_type=result.config.discussion_type,
                topic=result.config.topic,
                initiator=result.config.initiator,
                participants=result.config.participants,
                outcome=result.outcome,
                total_turns=len(result.turns),
                total_cost_usd=result.total_cost,
                duration_ms=duration_ms,
                synthesis=result.synthesis,
                transcript=result.transcript,
                action_task_ids=action_task_ids,
            )
        except Exception:
            log.exception("Failed to record discussion %s to DB", result.discussion_id)

        # Write markdown transcript
        if self.shared_dir:
            try:
                disc_dir = self.shared_dir / "discussions"
                disc_dir.mkdir(exist_ok=True)
                today = date.today().isoformat()
                filename = f"{today}-{result.discussion_id}.md"
                parts = [
                    f"# Discussion: {result.config.topic}",
                    f"Type: {result.config.discussion_type} | "
                    f"Initiated by: {result.config.initiator}",
                    f"Participants: {', '.join(result.config.participants)}",
                    f"Outcome: {result.outcome} | "
                    f"Cost: ${result.total_cost:.4f} | "
                    f"Turns: {len(result.turns)}",
                    "",
                    "---",
                    "",
                ]
                for t in result.turns:
                    parts.append(f"## Turn {t.turn_number} — {t.agent_name}")
                    parts.append(t.content)
                    parts.append("")

                if result.synthesis:
                    parts.append("## Synthesis")
                    parts.append(result.synthesis)

                (disc_dir / filename).write_text("\n".join(parts))
            except Exception:
                log.debug("Failed to write discussion transcript to markdown")

    def _update_discussion_post_synthesis(
        self, result: DiscussionResult, action_task_ids: list[str],
    ) -> None:
        """Update the DB record after synthesis + action spawning."""
        try:
            self.store.update_discussion_post_synthesis(
                discussion_id=result.discussion_id,
                synthesis=result.synthesis,
                action_task_ids=action_task_ids,
            )
        except Exception:
            log.exception(
                "Failed to update discussion %s post-synthesis",
                result.discussion_id,
            )
