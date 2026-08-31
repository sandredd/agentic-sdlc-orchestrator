

from orchestrator.contracts import Finding, Severity, StageOutcome
from orchestrator.core.gates import (
    ArtifactsProducedGate,
    DecisionsRecordedGate,
    EntryGate,
    GateDecision,
    PromisedOutputGate,
    RequiredContextGate,
    SeverityGate,
)
from orchestrator.core.graph import JoinPolicy, StageGraph
from orchestrator.core.ledger import EventType
from orchestrator.core.state import HaltReason, RunStatus, StageStatus

from .conftest import (
    RecordingExecutor,
    a_decision,
    code_artifact,
    fresh_state,
    result,
    stage,
)


def diamond():
    return StageGraph(
        [stage("a"), stage("b", ("a",)), stage("c", ("a",)), stage("d", ("b", "c"))]
    )


# -- happy path ------------------------------------------------------------


async def test_linear_run_succeeds_and_orders_stages(make_engine):
    graph = StageGraph([stage("a"), stage("b", ("a",)), stage("c", ("b",))])
    ex = RecordingExecutor()
    state = await make_engine(graph, ex).run(fresh_state())

    assert state.status is RunStatus.SUCCEEDED
    assert ex.started == ["a", "b", "c"]
    assert all(state.stage(n).status is StageStatus.SUCCEEDED for n in "abc")


async def test_independent_branches_run_concurrently(make_engine):
    ex = RecordingExecutor(delays={"b": 0.05, "c": 0.05})
    state = await make_engine(diamond(), ex).run(fresh_state())

    assert state.status is RunStatus.SUCCEEDED
    assert ex.concurrent_peak >= 2, "b and c must overlap, not run back to back"


async def test_all_join_is_a_synchronisation_barrier(make_engine):
    # c is slow; d must still wait for it even though b finished long ago.
    ex = RecordingExecutor(delays={"b": 0.0, "c": 0.08})
    await make_engine(diamond(), ex).run(fresh_state())

    assert ex.started[-1] == "d"
    assert not ex.started_before_end_of("d", "c"), "d must not start until c has finished"


async def test_fast_branch_is_not_held_by_slow_sibling(make_engine):
    # a -> slow, a -> fast -> fast2. fast2 must start before slow finishes:
    # that is the difference between frontier dispatch and layer lockstep.
    graph = StageGraph(
        [stage("a"), stage("slow", ("a",)), stage("fast", ("a",)), stage("fast2", ("fast",))]
    )
    ex = RecordingExecutor(delays={"slow": 0.15})
    await make_engine(graph, ex).run(fresh_state())

    assert ex.started_before_end_of("fast2", "slow")


async def test_max_parallel_stages_is_respected(make_engine):
    graph = StageGraph([stage("root"), *[stage(f"w{i}", ("root",)) for i in range(6)]])
    ex = RecordingExecutor(delays={f"w{i}": 0.03 for i in range(6)})
    await make_engine(graph, ex, max_parallel_stages=2).run(fresh_state())

    assert ex.concurrent_peak <= 2


async def test_any_join_starts_after_first_branch(make_engine):
    graph = StageGraph(
        [stage("a"), stage("b"), stage("join", ("a", "b"), join=JoinPolicy.ANY)]
    )
    ex = RecordingExecutor(delays={"b": 0.1})
    await make_engine(graph, ex).run(fresh_state())

    assert ex.started_before_end_of("join", "b")


# -- context and lineage ---------------------------------------------------


async def test_context_flows_between_stages_with_provenance(make_engine):
    graph = StageGraph(
        [
            stage("producer", produces=("spec",)),
            stage("consumer", ("producer",), consumes=("spec",)),
        ]
    )
    ex = RecordingExecutor(results={"producer": result("producer", context={"spec": {"v": 1}})})
    state = await make_engine(graph, ex).run(fresh_state())

    assert state.context.get("spec") == {"v": 1}
    assert state.context.writer_of("spec") == "producer"
    assert "consumer" in state.context.consumers_of("spec"), (
        "the engine must attribute reads even if the agent never asks"
    )


async def test_artifacts_are_written_sealed_and_indexed(make_engine):
    graph = StageGraph([stage("impl")])
    ex = RecordingExecutor(
        results={"impl": result("impl", artifacts=(code_artifact("src/app.py", "print(1)\n"),))}
    )
    engine = make_engine(graph, ex)
    state = await engine.run(fresh_state())

    assert engine.workspace.read("src/app.py") == "print(1)\n"
    assert len(state.artifacts["src/app.py"].content_hash) == 64


async def test_ledger_records_the_run_and_verifies(make_engine):
    engine = make_engine(diamond(), RecordingExecutor())
    await engine.run(fresh_state())

    types = [e.type for e in engine.ledger]
    assert types[0] is EventType.RUN_STARTED
    assert types[-1] is EventType.RUN_COMPLETED
    assert len(engine.ledger.of_type(EventType.STAGE_SUCCEEDED)) == 4
    assert engine.ledger.verify() == []


# -- entry gates -----------------------------------------------------------


class AlwaysBlock(EntryGate):
    name = "entry.always_block"

    def check(self, node, state):
        return GateDecision.block(self.name, "nope")


async def test_entry_gate_blocks_stage_and_halts_when_critical(make_engine):
    graph = StageGraph([stage("a", entry_gates=(AlwaysBlock(),))])
    state = await make_engine(graph, RecordingExecutor()).run(fresh_state())

    assert state.stage("a").status is StageStatus.BLOCKED
    assert state.status is RunStatus.HALTED
    assert state.halt_reason is HaltReason.BLOCKING_FAILURE


async def test_non_critical_entry_block_does_not_halt_the_run(make_engine):
    graph = StageGraph(
        [stage("a"), stage("opt", ("a",), critical=False, entry_gates=(AlwaysBlock(),))]
    )
    state = await make_engine(graph, RecordingExecutor()).run(fresh_state())

    assert state.stage("opt").status is StageStatus.BLOCKED
    assert state.status is RunStatus.FAILED, "non-critical failure fails but does not halt"
    assert state.halt_reason is None


async def test_required_context_gate_passes_when_upstream_produced(make_engine):
    graph = StageGraph(
        [
            stage("producer", produces=("spec",)),
            stage(
                "consumer",
                ("producer",),
                consumes=("spec",),
                entry_gates=(RequiredContextGate(),),
            ),
        ]
    )
    ex = RecordingExecutor(results={"producer": result("producer", context={"spec": 1})})
    state = await make_engine(graph, ex).run(fresh_state())
    assert state.status is RunStatus.SUCCEEDED


# -- exit gates ------------------------------------------------------------


async def test_promised_output_gate_rejects_a_silent_noop(make_engine):
    graph = StageGraph(
        [stage("a", produces=("spec",), exit_gates=(PromisedOutputGate(),))]
    )
    # The agent "succeeds" but produces nothing.
    ex = RecordingExecutor(results={"a": result("a")})
    engine = make_engine(graph, ex)
    state = await engine.run(fresh_state())

    assert state.stage("a").status is StageStatus.FAILED
    assert "did not produce" in state.stage("a").gate_failures[0]
    assert engine.ledger.of_type(EventType.GATE_EXIT_BLOCKED)


async def test_severity_gate_rejects_high_findings(make_engine):
    graph = StageGraph([stage("review", exit_gates=(SeverityGate(Severity.MEDIUM),))])
    ex = RecordingExecutor(
        results={
            "review": result("review").model_copy(
                update={
                    "findings": (
                        Finding(
                            severity=Severity.BLOCKER,
                            category="security",
                            summary="hardcoded credential",
                        ),
                    )
                }
            )
        }
    )
    state = await make_engine(graph, ex).run(fresh_state())
    assert state.stage("review").status is StageStatus.FAILED
    assert "hardcoded credential" in state.stage("review").gate_failures[0]


async def test_rejected_result_is_not_absorbed_into_run_state(make_engine):
    graph = StageGraph(
        [stage("a", produces=("spec",), exit_gates=(ArtifactsProducedGate(minimum=1),))]
    )
    ex = RecordingExecutor(results={"a": result("a", context={"spec": "leaked"})})
    state = await make_engine(graph, ex).run(fresh_state())

    assert not state.context.has("spec"), "output that failed a gate must not contaminate state"
    assert state.artifacts == {}


async def test_rejected_stage_rolls_the_workspace_back(make_engine):
    graph = StageGraph([stage("a", exit_gates=(DecisionsRecordedGate(),))])
    # Writes a file, but records no decision -> gate rejects -> file reverted.
    ex = RecordingExecutor(
        results={"a": result("a", artifacts=(code_artifact("half/done.py"),))}
    )
    engine = make_engine(graph, ex)
    state = await engine.run(fresh_state())

    assert state.stage("a").status is StageStatus.FAILED
    assert not engine.workspace.exists("half/done.py"), (
        "a stage must land completely or leave no trace"
    )
    assert engine.ledger.of_type(EventType.ROLLBACK_COMPLETED)


async def test_decisions_gate_accepts_recorded_rationale(make_engine):
    graph = StageGraph([stage("a", exit_gates=(DecisionsRecordedGate(),))])
    ex = RecordingExecutor(
        results={"a": result("a").model_copy(update={"decisions": (a_decision("a"),)})}
    )
    state = await make_engine(graph, ex).run(fresh_state())
    assert state.status is RunStatus.SUCCEEDED
    assert len(state.decisions) == 1


# -- failure propagation ---------------------------------------------------


async def test_failure_blocks_descendants_not_siblings(make_engine):
    graph = StageGraph(
        [
            stage("a"),
            stage("b", ("a",), critical=False),
            stage("b2", ("b",), critical=False),
            stage("c", ("a",), critical=False),
        ]
    )
    ex = RecordingExecutor(raises={"b": RuntimeError("boom")})
    state = await make_engine(graph, ex).run(fresh_state())

    assert state.stage("b").status is StageStatus.FAILED
    assert state.stage("b2").status is StageStatus.BLOCKED
    assert state.stage("c").status is StageStatus.SUCCEEDED, "sibling branch is unaffected"


async def test_stage_exception_is_recorded_with_type(make_engine):
    graph = StageGraph([stage("a", critical=False)])
    ex = RecordingExecutor(raises={"a": ValueError("bad input")})
    state = await make_engine(graph, ex).run(fresh_state())
    assert "ValueError: bad input" in state.stage("a").last_error


async def test_stage_timeout_fails_the_stage(make_engine):
    graph = StageGraph([stage("slow", critical=False)])
    ex = RecordingExecutor(delays={"slow": 1.0})
    state = await make_engine(graph, ex, stage_timeout_seconds=0.05).run(fresh_state())

    assert state.stage("slow").status is StageStatus.FAILED
    assert "timed out" in state.stage("slow").last_error


# -- safe stop -------------------------------------------------------------


async def test_run_deadline_triggers_safe_stop(make_engine):
    graph = StageGraph([stage("a"), stage("b", ("a",)), stage("c", ("b",))])
    ex = RecordingExecutor(delays={"a": 0.05, "b": 0.05, "c": 0.05})
    engine = make_engine(graph, ex, run_deadline_seconds=0.06)
    state = await engine.run(fresh_state())

    assert state.status is RunStatus.HALTED
    assert state.halt_reason is HaltReason.DEADLINE_EXCEEDED
    assert "c" not in ex.started, "no new stage may be dispatched after safe-stop"


async def test_operator_stop_drains_in_flight_work(make_engine):
    graph = StageGraph([stage("a"), stage("b", ("a",))])
    engine_holder = {}

    def hook(name):
        if name == "a":
            engine_holder["engine"].request_stop()

    ex = RecordingExecutor(delays={"a": 0.02}, hook=hook)
    engine = make_engine(graph, ex)
    engine_holder["engine"] = engine
    state = await engine.run(fresh_state())

    assert state.status is RunStatus.HALTED
    assert state.halt_reason is HaltReason.OPERATOR_STOP
    assert state.stage("a").status is StageStatus.SUCCEEDED, (
        "an in-flight stage is allowed to finish cleanly, not killed mid-write"
    )
    assert "b" not in ex.started


async def test_first_halt_reason_wins(make_engine):
    engine = make_engine(StageGraph([stage("a")]), RecordingExecutor())
    engine.request_stop(HaltReason.OPERATOR_STOP, "operator")
    engine.request_stop(HaltReason.DEADLINE_EXCEEDED, "deadline")
    state = await engine.run(fresh_state())

    assert state.halt_reason is HaltReason.OPERATOR_STOP
    assert state.halt_detail == "operator"


# -- approval and replan seams --------------------------------------------


async def test_needs_approval_fails_closed(make_engine):
    graph = StageGraph([stage("release")])
    ex = RecordingExecutor(
        results={
            "release": result("release").model_copy(
                update={"outcome": StageOutcome.NEEDS_APPROVAL}
            )
        }
    )
    engine = make_engine(graph, ex)
    state = await engine.run(fresh_state())

    assert state.stage("release").status is StageStatus.AWAITING_APPROVAL
    assert state.status is RunStatus.AWAITING_APPROVAL
    assert engine.ledger.of_type(EventType.APPROVAL_REQUESTED)


async def test_needs_replan_halts_for_replanning(make_engine):
    graph = StageGraph([stage("a"), stage("b", ("a",))])
    ex = RecordingExecutor(
        results={
            "a": result("a").model_copy(
                update={
                    "outcome": StageOutcome.NEEDS_REPLAN,
                    "replan_reason": "requirement changed",
                }
            )
        }
    )
    engine = make_engine(graph, ex)
    state = await engine.run(fresh_state())

    assert state.halt_reason is HaltReason.REPLAN_REQUIRED
    assert engine.ledger.of_type(EventType.REPLAN_TRIGGERED)
    assert "b" not in ex.started


# -- persistence -----------------------------------------------------------


async def test_state_is_persisted_and_reloadable(make_engine):
    from orchestrator.core.state import RunState

    engine = make_engine(diamond(), RecordingExecutor())
    state = await engine.run(fresh_state())

    reloaded = RunState.load(engine.state_path)
    assert reloaded.status is state.status
    assert reloaded.statuses() == state.statuses()
    assert reloaded.ledger_head == engine.ledger.head


# -- optional stages -------------------------------------------------------


async def test_optional_stage_failure_is_a_skip_and_the_run_still_succeeds(make_engine):
    graph = StageGraph([stage("a"), stage("polish", ("a",), optional=True)])
    ex = RecordingExecutor(raises={"polish": RuntimeError("nice-to-have failed")})
    state = await make_engine(graph, ex).run(fresh_state())

    assert state.stage("polish").status is StageStatus.SKIPPED
    assert state.status is RunStatus.SUCCEEDED


async def test_optional_stage_bypass_does_not_bury_its_dependents(make_engine):
    graph = StageGraph(
        [
            stage("a"),
            stage("opt", ("a",), optional=True),
            stage("after", ("opt",), critical=False),
        ]
    )
    ex = RecordingExecutor(raises={"opt": RuntimeError("skipped")})
    state = await make_engine(graph, ex).run(fresh_state())

    assert state.stage("opt").status is StageStatus.SKIPPED
    assert state.stage("after").status is StageStatus.SUCCEEDED


async def test_dependent_of_bypassed_stage_fails_precisely_on_its_missing_input(make_engine):
    # `after` genuinely needed `opt`'s output: it must fail at the context gate
    # with a specific reason, not be buried by a blanket structural block.
    graph = StageGraph(
        [
            stage("a"),
            stage("opt", ("a",), optional=True, produces=("polish",)),
            stage(
                "after",
                ("opt",),
                critical=False,
                consumes=("polish",),
                entry_gates=(RequiredContextGate(),),
            ),
        ]
    )
    ex = RecordingExecutor(raises={"opt": RuntimeError("skipped")})
    state = await make_engine(graph, ex).run(fresh_state())

    assert state.stage("after").status is StageStatus.BLOCKED
    assert "missing required context: polish" in state.stage("after").gate_failures[0]
