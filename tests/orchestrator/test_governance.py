"""Engine-level integration tests for Phase 3 governance: policy guardrails,
human approval checkpoints (including resume), cascading rollback, and
dynamic re-planning -- exercised through the real dispatch loop rather than
the unit level, since the whole point is that these compose correctly.
"""

from orchestrator.config import AutonomyLevel, RetryPolicy
from orchestrator.core.approvals import (
    ApprovalResponse,
    CallbackApprovalProvider,
    DenyAllProvider,
)
from orchestrator.core.gates import ArtifactsProducedGate
from orchestrator.core.graph import StageGraph
from orchestrator.core.ledger import EventType
from orchestrator.core.policy import ChangeControlPolicy, ChangeControlRule, PolicyEngine
from orchestrator.core.state import HaltReason, RunStatus, StageStatus

from .conftest import RecordingExecutor, code_artifact, fresh_state, result, stage

# -- policy guardrails, enforced universally --------------------------------


async def test_blocker_policy_violation_rejects_the_stage(make_engine):
    graph = StageGraph([stage("impl", critical=False)])
    ex = RecordingExecutor(
        results={
            "impl": result(
                "impl", artifacts=(code_artifact("app.py", 'password = "hunter2-real-value"\n'),)
            )
        }
    )
    engine = make_engine(graph, ex, retry=RetryPolicy(max_attempts=1))
    state = await engine.run(fresh_state())

    assert state.stage("impl").status is StageStatus.FAILED
    assert engine.ledger.of_type(EventType.POLICY_VIOLATION)
    assert not engine.workspace.exists("app.py"), "a blocked write must not land"


async def test_policy_findings_are_recorded_even_when_not_blocking(make_engine):
    graph = StageGraph([stage("impl")])
    ex = RecordingExecutor(
        results={"impl": result("impl", artifacts=(code_artifact("app.py", "x = 1\n"),))}
    )
    engine = make_engine(graph, ex)
    state = await engine.run(fresh_state())

    assert state.status is RunStatus.SUCCEEDED
    # CMP002 (tests-accompany-code) fires as MEDIUM, non-blocking, but recorded.
    assert any(f.category == "compliance" for f in state.findings)


async def test_change_control_blocks_frozen_path_regardless_of_node_gates(make_engine):
    graph = StageGraph([stage("release", critical=False)])
    ex = RecordingExecutor(
        results={
            "release": result(
                "release", artifacts=(code_artifact(".github/workflows/ci.yml", "name: ci\n"),)
            )
        }
    )
    policy = PolicyEngine(
        rules=[ChangeControlRule(ChangeControlPolicy(frozen_globs=("**/.github/**",)))]
    )
    engine = make_engine(graph, ex, retry=RetryPolicy(max_attempts=1))
    engine.policy_engine = policy
    state = await engine.run(fresh_state())

    assert state.stage("release").status is StageStatus.FAILED


# -- human approval checkpoints ----------------------------------------------


async def test_supervised_autonomy_requires_approval_before_landing(make_engine):
    # A pending (not auto-approving) provider, so the checkpoint actually
    # holds the run rather than resolving instantly.
    graph = StageGraph([stage("impl", produces=("built",))])
    ex = RecordingExecutor(
        results={"impl": result("impl", artifacts=(code_artifact("a.py"),), context={"built": 1})}
    )
    engine = make_engine(graph, ex, autonomy=AutonomyLevel.SUPERVISED)
    engine.approval_provider = CallbackApprovalProvider(lambda req: None)
    state = await engine.run(fresh_state())

    assert state.status is RunStatus.AWAITING_APPROVAL
    assert state.stage("impl").status is StageStatus.AWAITING_APPROVAL
    assert not state.context.has("built"), "nothing is absorbed until approved"
    assert engine.workspace.exists("a.py"), "artifact is written so the reviewer can see it"


async def test_resume_after_grant_lands_the_stage_without_rerunning_executor(make_engine):
    graph = StageGraph([stage("impl")])
    call_count = {"n": 0}

    async def executor(node, state):
        call_count["n"] += 1
        return result("impl", artifacts=(code_artifact("a.py", "v1\n"),))

    decisions = {"answer": None}
    provider = CallbackApprovalProvider(lambda req: decisions["answer"])
    engine = make_engine(graph, executor, autonomy=AutonomyLevel.SUPERVISED)
    engine.approval_provider = provider

    state = await engine.run(fresh_state())
    assert state.status is RunStatus.AWAITING_APPROVAL
    assert call_count["n"] == 1

    decisions["answer"] = ApprovalResponse(
        request_id=state.approval_log.pending()[0].id, granted=True, approver="alice"
    )
    state = await engine.resume(state)

    assert state.status is RunStatus.SUCCEEDED
    assert call_count["n"] == 1, "resume must not re-invoke the executor"
    assert state.context.get("impl") is None  # sanity: no such key produced
    assert "a.py" in state.artifacts
    assert engine.ledger.of_type(EventType.APPROVAL_GRANTED)


async def test_resume_after_rejection_fails_the_stage_and_halts(make_engine):
    graph = StageGraph([stage("impl", critical=False)])
    ex = RecordingExecutor(results={"impl": result("impl", artifacts=(code_artifact("a.py"),))})
    engine = make_engine(graph, ex, autonomy=AutonomyLevel.SUPERVISED)
    provider = CallbackApprovalProvider(lambda req: None)
    engine.approval_provider = provider

    state = await engine.run(fresh_state())
    assert state.status is RunStatus.AWAITING_APPROVAL

    provider.fn = lambda req: ApprovalResponse(
        request_id=req.id, granted=False, approver="bob", note="not ready"
    )
    state = await engine.resume(state)

    assert state.stage("impl").status is StageStatus.FAILED
    assert state.halt_reason is HaltReason.APPROVAL_REJECTED
    assert not engine.workspace.exists("a.py"), "a rejected result is rolled back"


async def test_deny_all_provider_halts_release_stage(make_engine):
    graph = StageGraph([stage("release", high_impact=True, critical=False)])
    ex = RecordingExecutor(results={"release": result("release")})
    engine = make_engine(graph, ex)
    engine.approval_provider = DenyAllProvider()
    state = await engine.run(fresh_state())

    assert state.stage("release").status is StageStatus.FAILED
    assert state.halt_reason is HaltReason.APPROVAL_REJECTED


async def test_high_impact_release_requires_approval_even_under_full_autonomy(make_engine):
    # With an auto-approving provider the run still completes -- being
    # "required" means the checkpoint is *consulted*, not that it blocks.
    # What must not happen is autonomy silently skipping the checkpoint.
    graph = StageGraph([stage("release", high_impact=True)])
    ex = RecordingExecutor(results={"release": result("release")})
    engine = make_engine(graph, ex, autonomy=AutonomyLevel.AUTONOMOUS)
    state = await engine.run(fresh_state())

    assert state.status is RunStatus.SUCCEEDED
    granted = engine.ledger.of_type(EventType.APPROVAL_GRANTED)
    assert granted, "raising autonomy must not switch off the checkpoint on a high-impact stage"
    assert granted[0].payload["automated"] is True

    # Now prove the checkpoint has real teeth: swap in a provider that denies.
    engine2 = make_engine(graph, ex, autonomy=AutonomyLevel.AUTONOMOUS)
    engine2.approval_provider = DenyAllProvider()
    state2 = await engine2.run(fresh_state())
    assert state2.stage("release").status is StageStatus.FAILED


# -- cascading rollback -------------------------------------------------------


async def test_downstream_of_a_later_failure_is_rolled_back(make_engine):
    graph = StageGraph(
        [
            stage("a", produces=("spec",)),
            stage("b", ("a",), consumes=("spec",), critical=False,
                  exit_gates=(ArtifactsProducedGate(minimum=1),)),
        ]
    )
    ex = RecordingExecutor(
        results={
            "a": result("a", artifacts=(code_artifact("shared.py", "v1\n"),), context={"spec": 1}),
            # b declares an artifact gate but produces none -> rejected -> exhausted -> FAILED
            "b": result("b"),
        }
    )
    engine = make_engine(graph, ex, retry=RetryPolicy(max_attempts=1))
    state = await engine.run(fresh_state())

    assert state.stage("b").status is StageStatus.FAILED
    # `a` is not a descendant of `b` and not coupled -> untouched.
    assert engine.workspace.exists("shared.py")


async def test_rollback_with_coupling_reverts_the_coupled_parallel_branch(make_engine):
    # `security` and `docs` are independent branches off the same root. If
    # `security` ultimately fails, `docs` -- explicitly coupled via
    # rollback_with -- must be reverted even though it already succeeded and
    # is not a descendant of `security`: the two were produced as a matched
    # pair and neither is trustworthy without the other.
    graph = StageGraph(
        [
            stage("impl", produces=("code",)),
            stage(
                "security", ("impl",), consumes=("code",), critical=False,
                rollback_with=frozenset({"docs"}),
                exit_gates=(ArtifactsProducedGate(minimum=1),),
            ),
            stage("docs", ("impl",), consumes=("code",)),
        ]
    )
    ex = RecordingExecutor(
        delays={"docs": 0.0, "security": 0.02},  # let docs land first
        results={
            "impl": result("impl", artifacts=(code_artifact("app.py"),), context={"code": 1}),
            "docs": result("docs", artifacts=(code_artifact("docs/README.md", "# hi\n"),)),
            "security": result("security"),  # no artifacts -> gate rejects -> fails
        },
    )
    engine = make_engine(graph, ex, retry=RetryPolicy(max_attempts=1))
    state = await engine.run(fresh_state())

    assert state.stage("security").status is StageStatus.FAILED
    assert state.stage("docs").status is StageStatus.ROLLED_BACK, (
        "docs was explicitly coupled to security and must not survive its failure"
    )
    assert not engine.workspace.exists("docs/README.md")
    assert engine.workspace.exists("app.py"), "impl is neither coupled nor downstream"
    assert state.status is RunStatus.FAILED


# -- replan --------------------------------------------------------------


async def test_replan_reruns_only_stale_consumers(make_engine):
    graph = StageGraph(
        [
            stage("req", produces=("spec",)),
            stage("design", ("req",), consumes=("spec",), produces=("blueprint",)),
            stage("impl", ("design",), consumes=("blueprint",)),
            stage("docs", ("req",), consumes=("spec",), optional=True),
        ]
    )
    calls: list[str] = []

    async def executor(node, state):
        calls.append(node.name)
        outputs = {k: {"rev": calls.count(node.name)} for k in node.produces}
        return result(node.name, context=outputs)

    engine = make_engine(graph, executor)
    state = await engine.run(fresh_state())
    assert state.status is RunStatus.SUCCEEDED
    first_pass = list(calls)
    assert first_pass.count("design") == 1

    calls.clear()
    state.context.set("spec", {"v": 2}, writer="req")  # simulate an upstream change
    state = await engine.replan(state, ["spec"], reason="human answered a blocking ambiguity")

    assert state.status is RunStatus.SUCCEEDED
    assert set(calls) == {"design", "impl", "docs"}, "only stale consumers/descendants rerun"
    assert "req" not in calls, "the stage that produced the changed value itself is not re-run"
    assert state.replan_count == 1


async def test_replan_limit_halts_instead_of_looping_forever(make_engine):
    graph = StageGraph(
        [stage("req", produces=("spec",)), stage("design", ("req",), consumes=("spec",))]
    )
    ex = RecordingExecutor(results={"req": result("req", context={"spec": 1})})
    engine = make_engine(graph, ex, max_replans=1)
    state = await engine.run(fresh_state())

    state.context.set("spec", 2, writer="req")
    state = await engine.replan(state, ["spec"])
    assert state.replan_count == 1

    state.context.set("spec", 3, writer="req")
    state = await engine.replan(state, ["spec"])
    assert state.replan_count == 1, "budget must not be exceeded"
    assert state.halt_reason is HaltReason.REPLAN_LIMIT_REACHED


async def test_governance_checkpoint_logs_exactly_one_request(make_engine):
    graph = StageGraph([stage("release", high_impact=True, critical=False)])
    ex = RecordingExecutor(results={"release": result("release")})
    engine = make_engine(graph, ex)
    engine.approval_provider = CallbackApprovalProvider(lambda req: None)
    state = await engine.run(fresh_state())

    requested = engine.ledger.of_type(EventType.APPROVAL_REQUESTED)
    assert len(requested) == 1, "the checkpoint must not double-log its own request"
    assert len(state.approval_log.requests) == 1
