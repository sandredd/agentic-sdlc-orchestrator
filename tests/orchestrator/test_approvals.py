from orchestrator.config import AutonomyLevel, OrchestratorConfig
from orchestrator.contracts import Finding, RiskLevel, Severity, StageResult
from orchestrator.core.approvals import (
    ApprovalLog,
    ApprovalPoint,
    ApprovalPolicy,
    ApprovalRequest,
    ApprovalResponse,
    AutoApproveProvider,
    CallbackApprovalProvider,
    DenyAllProvider,
    FileApprovalProvider,
)

from .conftest import fresh_state, stage


async def test_auto_approve_flags_itself_as_automated():
    provider = AutoApproveProvider()
    response = await provider.decide(
        ApprovalRequest(run_id="r", stage="release", point=ApprovalPoint.EXIT,
                        reason="x", risk=RiskLevel.LOW)
    )
    assert response.granted is True
    assert response.automated is True


async def test_deny_all_rejects():
    provider = DenyAllProvider()
    response = await provider.decide(
        ApprovalRequest(run_id="r", stage="release", point=ApprovalPoint.EXIT,
                        reason="x", risk=RiskLevel.LOW)
    )
    assert response.granted is False


async def test_callback_provider_can_return_pending():
    provider = CallbackApprovalProvider(lambda req: None)
    response = await provider.decide(
        ApprovalRequest(run_id="r", stage="release", point=ApprovalPoint.EXIT,
                        reason="x", risk=RiskLevel.LOW)
    )
    assert response is None


async def test_file_provider_writes_request_then_reads_response(tmp_path):
    provider = FileApprovalProvider(tmp_path)
    request = ApprovalRequest(
        run_id="r", stage="release", point=ApprovalPoint.EXIT, reason="ship it", risk=RiskLevel.HIGH
    )

    pending = await provider.decide(request)
    assert pending is None
    assert (tmp_path / f"{request.id}.request.json").exists()
    assert (tmp_path / f"{request.id}.brief.txt").exists()

    (tmp_path / f"{request.id}.response.json").write_text(
        ApprovalResponse(request_id=request.id, granted=True, approver="alice").model_dump_json()
    )
    decided = await provider.decide(request)
    assert decided is not None
    assert decided.granted is True
    assert decided.approver == "alice"


def test_brief_includes_key_fields():
    request = ApprovalRequest(
        run_id="r", stage="release", point=ApprovalPoint.EXIT, reason="risky",
        risk=RiskLevel.HIGH, summary="ships v2", artifact_paths=("a.py",),
    )
    brief = request.brief()
    assert "release" in brief
    assert "risky" in brief
    assert "a.py" in brief


# -- ApprovalLog -------------------------------------------------------------


def test_approval_log_tracks_pending_and_answered():
    log = ApprovalLog()
    req = ApprovalRequest(run_id="r", stage="release", point=ApprovalPoint.EXIT,
                          reason="x", risk=RiskLevel.LOW)
    log.record_request(req)
    assert log.pending_for("release") is req
    assert log.pending() == [req]

    log.record_response(ApprovalResponse(request_id=req.id, granted=True, approver="bob"))
    assert log.pending_for("release") is None
    assert log.pending() == []
    assert log.granted_count == 1
    assert log.rejected_count == 0


def test_human_count_excludes_automated_responses():
    log = ApprovalLog()
    req = ApprovalRequest(run_id="r", stage="a", point=ApprovalPoint.EXIT,
                          reason="x", risk=RiskLevel.LOW)
    log.record_request(req)
    log.record_response(
        ApprovalResponse(request_id=req.id, granted=True, approver="ci", automated=True)
    )
    assert log.human_count == 0


# -- ApprovalPolicy ------------------------------------------------------------


def result_with(findings=(), decisions=()):
    return StageResult(stage="x", findings=findings, decisions=decisions)


def test_high_impact_node_always_requires_approval_regardless_of_autonomy():
    cfg = OrchestratorConfig(autonomy=AutonomyLevel.AUTONOMOUS)
    policy = ApprovalPolicy(cfg)
    node = stage("release", high_impact=True, approval_point=ApprovalPoint.EXIT)
    req = policy.evaluate(node, fresh_state(), result_with(), ApprovalPoint.EXIT)
    assert bool(req) is True
    assert req.reason.startswith("release")


def test_autonomous_low_risk_stage_needs_no_approval():
    cfg = OrchestratorConfig(autonomy=AutonomyLevel.AUTONOMOUS)
    policy = ApprovalPolicy(cfg)
    node = stage("impl")
    req = policy.evaluate(node, fresh_state(), result_with(), ApprovalPoint.EXIT)
    assert bool(req) is False


def test_supervised_autonomy_requires_approval_on_every_exit():
    cfg = OrchestratorConfig(autonomy=AutonomyLevel.SUPERVISED)
    policy = ApprovalPolicy(cfg)
    node = stage("impl")
    req = policy.evaluate(node, fresh_state(), result_with(), ApprovalPoint.EXIT)
    assert bool(req) is True


def test_bounded_autonomy_escalates_only_above_threshold():
    cfg = OrchestratorConfig(
        autonomy=AutonomyLevel.BOUNDED, approval_risk_threshold=RiskLevel.HIGH
    )
    policy = ApprovalPolicy(cfg)
    node = stage("impl")

    low = result_with()
    assert bool(policy.evaluate(node, fresh_state(), low, ApprovalPoint.EXIT)) is False

    blocker_finding = result_with(
        findings=(Finding(severity=Severity.BLOCKER, category="security", summary="bad"),)
    )
    assert bool(policy.evaluate(node, fresh_state(), blocker_finding, ApprovalPoint.EXIT)) is True


def test_risk_assessment_escalates_from_findings():
    cfg = OrchestratorConfig()
    policy = ApprovalPolicy(cfg)
    node = stage("impl")
    high_finding = result_with(
        findings=(Finding(severity=Severity.HIGH, category="security", summary="bad"),)
    )
    risk = policy.assess_risk(node, fresh_state(), high_finding)
    assert risk is RiskLevel.HIGH
