"""Full 9-agent SDLC pipeline through the real Engine -- the Phase 4 proof
that requirement understanding, decomposition, implementation, real test
execution, security review, docs, risk synthesis and a gated release all
compose into one coherent, auditable run."""

from pathlib import Path

from orchestrator.agents import build_graph, make_executor
from orchestrator.config import AutonomyLevel, OrchestratorConfig
from orchestrator.contracts import Requirement, ScenarioKind
from orchestrator.core.approvals import ApprovalResponse, CallbackApprovalProvider
from orchestrator.core.engine import Engine
from orchestrator.core.state import RunState, RunStatus, StageStatus
from orchestrator.core.workspace import Workspace


def _engine(tmp_path: Path, *, autonomy=AutonomyLevel.BOUNDED) -> Engine:
    ws = Workspace(tmp_path / "ws")
    graph = build_graph()
    executor = make_executor(workspace=ws)
    cfg = OrchestratorConfig(run_root=tmp_path / "runs", autonomy=autonomy)
    engine = Engine(graph, executor, config=cfg, workspace=ws)
    engine.approval_provider = CallbackApprovalProvider(
        lambda req: ApprovalResponse(request_id=req.id, granted=True, approver="reviewer")
    )
    return engine


async def test_greenfield_run_succeeds_with_a_working_generated_service(tmp_path):
    engine = _engine(tmp_path)
    req = Requirement(
        title="URL Shortener",
        statement="Build a URL shortener with core APIs, custom aliases, expiration, "
        "click analytics, and rate limiting.",
        kind=ScenarioKind.GREENFIELD,
    )
    state = await engine.run(RunState(run_id="e2e", requirement=req))

    assert state.status is RunStatus.SUCCEEDED
    for name in ("requirements", "architecture", "planning", "implementation",
                 "testing", "security", "validation", "release"):
        assert state.stage(name).status is StageStatus.SUCCEEDED

    assert engine.workspace.exists("app/main.py")
    assert engine.workspace.exists("tests/test_api.py")
    test_report = state.context.get("test_report")
    assert test_report["ran"] is True
    assert test_report["failed"] == 0
    assert test_report["passed"] > 0

    assert engine.ledger.verify() == []
    report = engine.metrics(state)
    assert report.success_rate == 1.0


async def test_ambiguous_requirement_halts_before_implementation(tmp_path):
    engine = _engine(tmp_path)
    req = Requirement(
        title="Improve it", statement="Make it better, TBD on details",
        kind=ScenarioKind.AMBIGUOUS,
    )
    state = await engine.run(RunState(run_id="ambiguous", requirement=req))

    # architecture/planning both gate on NoBlockingAmbiguityGate and must not run;
    # implementation is then unreachable since neither of its dependencies can
    # ever satisfy, and is correctly propagated to BLOCKED rather than left
    # dangling in PENDING forever.
    assert state.stage("architecture").status is StageStatus.BLOCKED
    assert state.stage("planning").status is StageStatus.BLOCKED
    assert state.stage("implementation").status is StageStatus.BLOCKED
    assert not engine.workspace.exists("app/main.py")


async def test_release_is_a_mandatory_checkpoint_even_under_autonomous(tmp_path):
    engine = _engine(tmp_path, autonomy=AutonomyLevel.AUTONOMOUS)
    req = Requirement(
        title="URL Shortener", statement="Build a URL shortener with core APIs.",
        kind=ScenarioKind.GREENFIELD,
    )
    await engine.run(RunState(run_id="autonomous", requirement=req))

    from orchestrator.core.ledger import EventType

    granted = engine.ledger.of_type(EventType.APPROVAL_GRANTED)
    assert granted, "release must always go through a checkpoint, regardless of autonomy"
    assert any(e.stage == "release" for e in granted)


async def test_run_is_a_reproducible_deterministic_diff(tmp_path):
    """Same requirement, deterministic provider, twice -> byte-identical code."""
    req = Requirement(
        title="URL Shortener", statement="Build a URL shortener with custom aliases.",
        kind=ScenarioKind.GREENFIELD,
    )
    engine1 = _engine(tmp_path / "run1")
    state1 = await engine1.run(RunState(run_id="r1", requirement=req))
    engine2 = _engine(tmp_path / "run2")
    state2 = await engine2.run(RunState(run_id="r2", requirement=req))

    assert engine1.workspace.read("app/routes.py") == engine2.workspace.read("app/routes.py")
    assert state1.artifacts.keys() == state2.artifacts.keys()
