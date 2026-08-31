from orchestrator.core.engine import Engine
from orchestrator.core.graph import StageGraph
from orchestrator.core.ledger import Ledger
from orchestrator.core.metrics import compute
from orchestrator.core.state import RunState
from orchestrator.core.workspace import Workspace

from .conftest import RecordingExecutor, fresh_state, stage


async def test_all_succeed_gives_full_success_rate(tmp_path):
    graph = StageGraph([stage("a"), stage("b", ("a",))])
    engine = Engine(
        graph, RecordingExecutor(), run_id="r",
        workspace=Workspace(tmp_path / "ws"),
        ledger=Ledger("r", path=tmp_path / "l.jsonl"),
    )
    state = await engine.run(fresh_state())
    report = compute(state, engine.ledger)

    assert report.success_rate == 1.0
    assert report.retry_count == 0
    assert report.rollback_count == 0
    assert report.end_to_end_latency_seconds is not None
    assert report.end_to_end_latency_seconds >= 0


async def test_skipped_optional_stage_excluded_from_denominator(tmp_path):
    graph = StageGraph([stage("a"), stage("polish", ("a",), optional=True)])
    ex = RecordingExecutor(raises={"polish": RuntimeError("nope")})
    engine = Engine(
        graph, ex, run_id="r",
        workspace=Workspace(tmp_path / "ws"),
        ledger=Ledger("r", path=tmp_path / "l.jsonl"),
    )
    state = await engine.run(fresh_state())
    report = compute(state, engine.ledger)

    assert report.skipped == 1
    assert report.success_rate == 1.0, "a deliberate skip is not a reliability failure"


def test_report_serialises_to_plain_dict():
    import json

    state = RunState(run_id="r", requirement=fresh_state().requirement)
    ledger = Ledger("r")
    report = compute(state, ledger)
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["run_id"] == "r"
    assert payload["stages"] == []


def test_summary_line_handles_missing_latency_gracefully():
    state = RunState(run_id="r", requirement=fresh_state().requirement)
    report = compute(state, Ledger("r"))
    line = report.summary_line()
    assert "success_rate=" in line
    assert "e2e=n/a" in line
