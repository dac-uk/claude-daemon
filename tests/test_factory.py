"""Tests for the Software Factory (plan / build / review).

Covers the public surface of SoftwareFactory plus its integration points
in the Orchestrator tag pipeline and the daemon back-compat shim. The
factory's subprocess (``git diff``) is never actually invoked — tests
monkeypatch ``SoftwareFactory._generate_diff`` with a canned string.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_daemon.agents.registry import AgentRegistry
from claude_daemon.agents.workflow import WorkflowEngine
from claude_daemon.core.process import ClaudeResponse
from claude_daemon.factory import (
    FactoryConfig,
    ReviewPreset,
    SoftwareFactory,
)
from claude_daemon.memory.store import ConversationStore
from claude_daemon.orchestration import TaskAPI
from claude_daemon.orchestration.approvals import ApprovalsStore


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


@pytest.fixture
def registry(agents_dir: Path) -> AgentRegistry:
    reg = AgentRegistry(agents_dir)
    reg.create_agent("albert", role="CIO", is_orchestrator=True)
    reg.create_agent("luna", role="Designer")
    reg.create_agent("max", role="Reviewer")
    return reg


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    s = ConversationStore(tmp_path / "test.db")
    yield s
    s.close()


def _ok(text: str, cost: float = 0.01) -> ClaudeResponse:
    return ClaudeResponse(
        result=text, session_id="test",
        cost=cost, input_tokens=10, output_tokens=5,
        num_turns=1, duration_ms=100, is_error=False,
    )


def _err(text: str, cost: float = 0.0) -> ClaudeResponse:
    return ClaudeResponse(
        result=text, session_id="test",
        cost=cost, input_tokens=10, output_tokens=5,
        num_turns=1, duration_ms=100, is_error=True,
    )


@pytest.fixture
def orchestrator(registry: AgentRegistry):
    orch = MagicMock()
    orch._spawned_tasks = {}
    orch.hub = None
    orch.registry = registry
    orch.spawn_task = MagicMock()

    async def _send(agent, prompt, platform="workflow",
                    user_id="workflow", task_type="default"):
        return _ok(f"[{agent.name}] {prompt[:40]}")

    orch.send_to_agent = AsyncMock(side_effect=_send)
    return orch


@pytest.fixture
def workflow(orchestrator, registry):
    return WorkflowEngine(orchestrator, registry)


@pytest.fixture
def task_api(orchestrator, registry, store):
    approvals = ApprovalsStore(store)
    return TaskAPI(
        orchestrator=orchestrator, registry=registry, store=store,
        approvals_store=approvals,
    ), approvals


@pytest.fixture
def factory_config(tmp_path: Path) -> FactoryConfig:
    return FactoryConfig(
        planner_agent="auto",
        executor_agents=["luna"],
        reviewer_agent="max",
        review_max_iterations=2,
        plans_dir=tmp_path / "plans",
        require_plan_approval=True,
    )


@pytest.fixture
def factory(orchestrator, workflow, registry, store, task_api, factory_config):
    api, approvals = task_api
    return SoftwareFactory(
        orchestrator=orchestrator,
        workflow_engine=workflow,
        registry=registry,
        store=store,
        config=factory_config,
        task_api=api,
        approvals_store=approvals,
    )


# ── FactoryConfig ────────────────────────────────────────────────


class TestFactoryConfig:
    def test_defaults(self):
        cfg = FactoryConfig()
        assert cfg.planner_agent == "orchestrator"
        assert cfg.executor_agents == ["auto"]
        assert cfg.reviewer_agent == "auto"
        assert cfg.review_max_iterations == 3
        assert cfg.require_plan_approval is True
        assert len(cfg.review_presets) == 4  # bugs / security / perf / quality
        focuses = {p.focus for p in cfg.review_presets}
        assert focuses == {"bugs", "security", "performance", "quality"}

    def test_from_dict(self, tmp_path: Path):
        cfg = FactoryConfig.from_dict(
            {
                "planner_agent": "albert",
                "executor_agents": ["luna", "max"],
                "reviewer_agent": "albert",
                "review_max_iterations": 5,
                "require_plan_approval": False,
                "plans_dir": "my-plans",
                "review_presets": [
                    {"focus": "bugs", "prompt": "Find bugs.", "agent": "max"},
                ],
            },
            data_dir=tmp_path,
        )
        assert cfg.planner_agent == "albert"
        assert cfg.executor_agents == ["luna", "max"]
        assert cfg.reviewer_agent == "albert"
        assert cfg.review_max_iterations == 5
        assert cfg.require_plan_approval is False
        assert cfg.plans_dir == tmp_path / "my-plans"
        assert len(cfg.review_presets) == 1
        assert cfg.review_presets[0].focus == "bugs"
        assert cfg.review_presets[0].agent == "max"


# ── plan() ───────────────────────────────────────────────────────


class TestFactoryPlan:
    @pytest.mark.asyncio
    async def test_empty_request(self, factory):
        result = await factory.plan("   ")
        assert result.status == "error"
        assert "empty" in result.error.lower()

    @pytest.mark.asyncio
    async def test_writes_spec_file(self, factory, factory_config):
        result = await factory.plan("Add healthcheck endpoint")
        assert result.plan_path.exists()
        content = result.plan_path.read_text()
        assert "Add healthcheck endpoint" in content
        # Planner's response is echoed into the plan
        assert result.plan_content
        assert result.status == "pending_approval"

    @pytest.mark.asyncio
    async def test_auto_planner_resolves_to_orchestrator(
        self, factory, registry, orchestrator,
    ):
        await factory.plan("Add X")
        # albert is the orchestrator agent in this test registry
        sent_agent = orchestrator.send_to_agent.call_args.kwargs.get("agent")
        if sent_agent is None:
            sent_agent = orchestrator.send_to_agent.call_args.args[0]
        assert sent_agent.name == "albert"

    @pytest.mark.asyncio
    async def test_submits_via_task_api_with_require_approval(
        self, factory, store,
    ):
        result = await factory.plan("Build a dashboard")
        # TaskAPI path: a task_queue row is created with initial_status
        # ``pending_approval`` and an approvals row is linked to it.
        row = store.get_task(result.task_id)
        assert row is not None
        assert row["status"] == "pending_approval"
        assert row["source"] == "factory"
        assert result.approval_id is not None
        # And the orchestrator's spawn_task was NOT called (we're waiting
        # on approval before spawning).
        factory.orchestrator.spawn_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_auto_create_goal(self, factory, store):
        # Factory never creates Goal rows; it only links via goal_id.
        result = await factory.plan("Build X", goal_id=None)
        row = store.get_task(result.task_id)
        assert row is not None
        assert row.get("goal_id") in (None, 0)

    @pytest.mark.asyncio
    async def test_planner_error_returns_error_status(
        self, factory, orchestrator,
    ):
        orchestrator.send_to_agent = AsyncMock(
            return_value=_err("planner blew up"),
        )
        result = await factory.plan("Oops")
        assert result.status == "error"
        assert "planner" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_approval_when_disabled(
        self, factory_config, orchestrator, workflow, registry, store,
    ):
        factory_config.require_plan_approval = False
        approvals = ApprovalsStore(store)
        api = TaskAPI(
            orchestrator=orchestrator, registry=registry, store=store,
            approvals_store=approvals,
        )
        f = SoftwareFactory(
            orchestrator=orchestrator, workflow_engine=workflow,
            registry=registry, store=store, config=factory_config,
            task_api=api, approvals_store=approvals,
        )
        result = await f.plan("Quick plan")
        assert result.task_id == ""
        assert result.approval_id is None


# ── build() ──────────────────────────────────────────────────────


class TestFactoryBuild:
    @pytest.mark.asyncio
    async def test_auto_plans_when_no_path(
        self, factory, orchestrator, registry,
    ):
        # Make the reviewer always PASS so the loop terminates quickly.
        async def _send(agent, prompt, **kw):
            if agent.name == "max":
                return _ok("PASS looks great")
            return _ok(f"[{agent.name}] built {prompt[:20]}")

        orchestrator.send_to_agent = AsyncMock(side_effect=_send)
        result = await factory.build("Add button")
        assert result.success
        assert result.plan_path is not None
        assert result.plan_path.exists()
        # First call is the auto-plan (sent to orchestrator = albert)
        first_call = orchestrator.send_to_agent.call_args_list[0]
        first_agent = first_call.kwargs.get("agent") or first_call.args[0]
        assert first_agent.name == "albert"

    @pytest.mark.asyncio
    async def test_uses_existing_plan_path(
        self, factory, orchestrator, tmp_path: Path,
    ):
        plan_file = tmp_path / "myplan.md"
        plan_file.write_text("# Plan: Do the thing\n\nSteps: {code: 'x'}")

        async def _send(agent, prompt, **kw):
            if agent.name == "max":
                return _ok("PASS")
            return _ok(f"[{agent.name}] built")

        orchestrator.send_to_agent = AsyncMock(side_effect=_send)
        result = await factory.build("Do the thing", plan_path=plan_file)
        assert result.success
        # No auto-plan call — first call is the executor (luna)
        first_call = orchestrator.send_to_agent.call_args_list[0]
        first_agent = first_call.kwargs.get("agent") or first_call.args[0]
        assert first_agent.name == "luna"
        # The plan content (including literal braces in the 'code: x'
        # block) must have been safely interpolated — no KeyError raised.

    @pytest.mark.asyncio
    async def test_skip_plan_bypasses_planner(
        self, factory, orchestrator,
    ):
        async def _send(agent, prompt, **kw):
            if agent.name == "max":
                return _ok("PASS")
            return _ok(f"[{agent.name}] built")

        orchestrator.send_to_agent = AsyncMock(side_effect=_send)
        result = await factory.build("Skip planning", skip_plan=True)
        assert result.success
        # First call is executor, not planner.
        first_call = orchestrator.send_to_agent.call_args_list[0]
        first_agent = first_call.kwargs.get("agent") or first_call.args[0]
        assert first_agent.name == "luna"

    @pytest.mark.asyncio
    async def test_review_loop_retries_on_fail(
        self, factory, orchestrator,
    ):
        # First review FAILs, second PASSes. Verifies the review loop
        # runs more than one iteration.
        review_responses = iter(["FAIL missing tests", "PASS now good"])

        async def _send(agent, prompt, **kw):
            if agent.name == "max":
                return _ok(next(review_responses))
            return _ok(f"[{agent.name}] built")

        orchestrator.send_to_agent = AsyncMock(side_effect=_send)
        result = await factory.build("Feature with retry", skip_plan=True)
        assert result.success
        assert result.iterations >= 2

    @pytest.mark.asyncio
    async def test_writes_result_artifact(
        self, factory, orchestrator,
    ):
        async def _send(agent, prompt, **kw):
            if agent.name == "max":
                return _ok("PASS")
            return _ok(f"[{agent.name}] done")

        orchestrator.send_to_agent = AsyncMock(side_effect=_send)
        result = await factory.build("Write me a result", skip_plan=True)
        assert result.result_path is not None
        assert result.result_path.exists()
        body = result.result_path.read_text()
        assert "Build Result" in body
        assert "Write me a result" in body

    @pytest.mark.asyncio
    async def test_empty_request(self, factory):
        result = await factory.build("   ")
        assert not result.success
        assert "empty" in result.error.lower()


# ── review() ─────────────────────────────────────────────────────


class TestFactoryReview:
    @pytest.mark.asyncio
    async def test_empty_diff_returns_early(
        self, factory, monkeypatch,
    ):
        async def _no_diff(target):
            return ""

        monkeypatch.setattr(factory, "_generate_diff", _no_diff)
        result = await factory.review("HEAD")
        assert result.error == "empty-diff"
        assert result.findings == []

    @pytest.mark.asyncio
    async def test_parallel_spawns_presets(
        self, factory, orchestrator, monkeypatch,
    ):
        async def _diff(target):
            return "diff --git a b\n+added line\n"

        monkeypatch.setattr(factory, "_generate_diff", _diff)

        async def _send(agent, prompt, **kw):
            # Return a canned HIGH severity finding so severity counts
            # accumulate predictably.
            return _ok("- HIGH bug at file.py:10 — thing")

        orchestrator.send_to_agent = AsyncMock(side_effect=_send)
        result = await factory.review("HEAD")
        # Default config ships 4 review presets.
        assert len(result.findings) == 4
        assert result.severity_counts["high"] >= 4
        assert result.report_path is not None
        assert result.report_path.exists()
        report = result.report_path.read_text()
        assert "Severity Counts" in report
        assert "High:" in report

    @pytest.mark.asyncio
    async def test_diff_with_braces_does_not_crash_template(
        self, factory, orchestrator, monkeypatch,
    ):
        # A diff containing literal {foo} tokens would trip the
        # WorkflowEngine's .format() call if not escaped.
        async def _diff(target):
            return "def f(): return {'key': 'val'}  # {unclosed"

        monkeypatch.setattr(factory, "_generate_diff", _diff)

        async def _send(agent, prompt, **kw):
            return _ok("- LOW nit at x.py:1")

        orchestrator.send_to_agent = AsyncMock(side_effect=_send)
        result = await factory.review("HEAD")
        assert result.error == ""
        assert len(result.findings) > 0

    @pytest.mark.asyncio
    async def test_preset_respects_explicit_agent(
        self, factory_config, orchestrator, workflow, registry, store,
        monkeypatch,
    ):
        # Pin every preset to luna — they should all go there.
        factory_config.review_presets = [
            ReviewPreset(focus="bugs", prompt="Find bugs.", agent="luna"),
        ]
        approvals = ApprovalsStore(store)
        api = TaskAPI(
            orchestrator=orchestrator, registry=registry, store=store,
            approvals_store=approvals,
        )
        f = SoftwareFactory(
            orchestrator=orchestrator, workflow_engine=workflow,
            registry=registry, store=store, config=factory_config,
            task_api=api, approvals_store=approvals,
        )

        async def _diff(target):
            return "diff\n+line"

        monkeypatch.setattr(f, "_generate_diff", _diff)

        async def _send(agent, prompt, **kw):
            return _ok(f"[{agent.name}]")

        orchestrator.send_to_agent = AsyncMock(side_effect=_send)
        result = await f.review("HEAD")
        assert len(result.findings) == 1
        assert result.findings[0].agent_name == "luna"


# ── Orchestrator tag integration ─────────────────────────────────


class TestOrchestratorTags:
    @pytest.mark.asyncio
    async def test_build_tag_dispatches_to_factory(
        self, registry, store,
    ):
        from claude_daemon.agents.orchestrator import Orchestrator

        orch = Orchestrator(
            registry=registry,
            process_manager=MagicMock(),
            store=store,
        )
        fake_factory = MagicMock()
        fake_factory.build = AsyncMock(return_value=MagicMock(
            slug="slug-1", summary="built", success=True,
        ))
        fake_factory.plan = AsyncMock()
        fake_factory.review = AsyncMock()
        orch.set_factory(fake_factory)

        albert = registry.get("albert")
        response = _ok("[BUILD] add healthcheck endpoint")
        out = await orch._process_factory_requests(
            albert, response, platform="cli",
        )
        fake_factory.build.assert_awaited_once()
        first_arg = fake_factory.build.await_args.args[0]
        assert "healthcheck" in first_arg
        assert "Build Result" in out.result

    @pytest.mark.asyncio
    async def test_plan_tag_dispatches_to_factory(
        self, registry, store,
    ):
        from claude_daemon.agents.orchestrator import Orchestrator

        orch = Orchestrator(
            registry=registry, process_manager=MagicMock(), store=store,
        )
        fake_factory = MagicMock()
        fake_factory.plan = AsyncMock(return_value=MagicMock(
            slug="s", plan_path=Path("/tmp/p.md"),
            plan_content="plan body",
            approval_id=42, status="pending_approval",
        ))
        fake_factory.build = AsyncMock()
        fake_factory.review = AsyncMock()
        orch.set_factory(fake_factory)

        albert = registry.get("albert")
        response = _ok("[PLAN] design auth layer")
        out = await orch._process_factory_requests(
            albert, response, platform="cli",
        )
        fake_factory.plan.assert_awaited_once()
        assert "Plan Created" in out.result
        assert "approval_id=42" in out.result

    @pytest.mark.asyncio
    async def test_review_tag_dispatches_to_factory(
        self, registry, store,
    ):
        from claude_daemon.agents.orchestrator import Orchestrator

        orch = Orchestrator(
            registry=registry, process_manager=MagicMock(), store=store,
        )
        fake_factory = MagicMock()
        fake_factory.review = AsyncMock(return_value=MagicMock(
            slug="rs", summary="0 critical, 0 high, ...",
        ))
        fake_factory.build = AsyncMock()
        fake_factory.plan = AsyncMock()
        orch.set_factory(fake_factory)

        albert = registry.get("albert")
        response = _ok("[REVIEW] current branch")
        out = await orch._process_factory_requests(
            albert, response, platform="cli",
        )
        fake_factory.review.assert_awaited_once()
        assert "Review Report" in out.result

    @pytest.mark.asyncio
    async def test_tags_in_code_blocks_are_ignored(
        self, registry, store,
    ):
        from claude_daemon.agents.orchestrator import Orchestrator

        orch = Orchestrator(
            registry=registry, process_manager=MagicMock(), store=store,
        )
        fake_factory = MagicMock()
        fake_factory.build = AsyncMock()
        fake_factory.plan = AsyncMock()
        fake_factory.review = AsyncMock()
        orch.set_factory(fake_factory)

        albert = registry.get("albert")
        # Tag lives inside a fenced code block — must be skipped.
        response = _ok("Here is an example:\n```\n[BUILD] example\n```\n")
        await orch._process_factory_requests(
            albert, response, platform="cli",
        )
        fake_factory.build.assert_not_awaited()


# ── Recursion guard ──────────────────────────────────────────────


class TestRecursionGuard:
    @pytest.mark.asyncio
    async def test_factory_not_invoked_inside_council(
        self, registry, store,
    ):
        """Audit Fix #3: factory tags emitted during council/discussion
        turns must be IGNORED, not re-dispatched, to prevent runaway
        recursion where a factory build produces text that re-triggers
        the factory.
        """
        from claude_daemon.agents.orchestrator import Orchestrator

        orch = Orchestrator(
            registry=registry, process_manager=MagicMock(), store=store,
        )
        fake_factory = MagicMock()
        fake_factory.build = AsyncMock()
        fake_factory.plan = AsyncMock()
        fake_factory.review = AsyncMock()
        orch.set_factory(fake_factory)

        albert = registry.get("albert")
        response = _ok("[BUILD] tries to recurse")

        # _process_delegations is the gatekeeper that runs the
        # platform-guarded factory dispatch.
        await orch._process_delegations(
            albert, response, platform="council",
        )
        fake_factory.build.assert_not_awaited()

        # Sanity check: in "cli" platform, the same pipeline DOES fire.
        fake_factory.build = AsyncMock(return_value=MagicMock(
            slug="s", summary="ok", success=True,
        ))
        response2 = _ok("[BUILD] ordinary request")
        await orch._process_delegations(
            albert, response2, platform="cli",
        )
        fake_factory.build.assert_awaited_once()


# ── Back-compat: daemon.run_build_workflow ───────────────────────


class TestBackCompatShim:
    @pytest.mark.asyncio
    async def test_run_build_workflow_delegates_to_factory(
        self, registry,
    ):
        """Audit Fix #1: daemon.run_build_workflow must now be a thin
        shim that calls SoftwareFactory.build() with the historical
        albert/luna/max role config. No duplicate review-loop logic.
        """
        from claude_daemon.core.daemon import ClaudeDaemon

        # Build a bare ClaudeDaemon shell with just the fields the shim
        # reads. The shim touches self.factory, self.agent_registry,
        # and temporarily mutates self.factory.config.reviewer_agent.
        daemon = ClaudeDaemon.__new__(ClaudeDaemon)
        daemon.agent_registry = registry

        fake_factory = MagicMock()
        fake_factory.config = MagicMock(reviewer_agent="auto")
        fake_factory.build = AsyncMock(return_value=MagicMock(
            success=True,
            summary="  1. [PASS] luna",
            final_output="done",
        ))
        daemon.factory = fake_factory

        out = await daemon.run_build_workflow("build a thing", max_total_cost=1.0)
        fake_factory.build.assert_awaited_once()
        kwargs = fake_factory.build.await_args.kwargs
        # Historical role config preserved
        assert kwargs["executor_agents"] == ["albert", "luna"]
        assert kwargs["max_total_cost"] == 1.0
        assert kwargs["skip_plan"] is True
        # Reviewer was temporarily pointed at max, then restored
        assert fake_factory.config.reviewer_agent == "auto"
        assert "PASSED" in out

    @pytest.mark.asyncio
    async def test_run_build_workflow_without_factory_returns_message(
        self, registry,
    ):
        from claude_daemon.core.daemon import ClaudeDaemon

        daemon = ClaudeDaemon.__new__(ClaudeDaemon)
        daemon.agent_registry = registry
        daemon.factory = None
        out = await daemon.run_build_workflow("build")
        assert "not initialized" in out.lower()
