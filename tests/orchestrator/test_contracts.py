import pytest
from pydantic import ValidationError

from orchestrator.contracts import (
    Ambiguity,
    Artifact,
    ArtifactKind,
    Finding,
    NormalizedRequirement,
    RiskLevel,
    Severity,
    StageResult,
)


def test_frozen_contracts_reject_mutation():
    art = Artifact(path="a.py", kind=ArtifactKind.CODE, content="x")
    with pytest.raises(ValidationError):
        art.path = "b.py"


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        Artifact(path="a.py", kind=ArtifactKind.CODE, content="x", sneaky=True)


def test_artifact_hash_is_content_addressed():
    a = Artifact(path="a.py", kind=ArtifactKind.CODE, content="same").with_hash()
    b = Artifact(path="b.py", kind=ArtifactKind.CODE, content="same").with_hash()
    c = Artifact(path="a.py", kind=ArtifactKind.CODE, content="other").with_hash()
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


def test_severity_and_risk_ordering():
    assert Severity.BLOCKER.rank > Severity.HIGH.rank > Severity.INFO.rank
    assert RiskLevel.CRITICAL.rank > RiskLevel.LOW.rank


def test_stage_result_surfaces_blockers_and_max_severity():
    result = StageResult(
        stage="review",
        findings=(
            Finding(severity=Severity.LOW, category="style", summary="naming"),
            Finding(severity=Severity.BLOCKER, category="security", summary="secret in repo"),
        ),
    )
    assert len(result.blockers) == 1
    assert result.max_severity() is Severity.BLOCKER


def test_empty_findings_max_severity_is_info():
    assert StageResult(stage="x").max_severity() is Severity.INFO


def test_blocking_ambiguities_are_isolated():
    nreq = NormalizedRequirement(
        source_requirement_id="req_1",
        problem_statement="p",
        in_scope=(),
        out_of_scope=(),
        functional=(),
        non_functional=(),
        acceptance=(),
        ambiguities=(
            Ambiguity(question="q1", why_it_matters="w", assumption="a", confidence=0.9),
            Ambiguity(
                question="q2", why_it_matters="w", assumption="a", confidence=0.3, blocking=True
            ),
        ),
    )
    assert len(nreq.blocking_ambiguities) == 1
    assert nreq.blocking_ambiguities[0].question == "q2"


def test_confidence_is_bounded():
    with pytest.raises(ValidationError):
        Ambiguity(question="q", why_it_matters="w", assumption="a", confidence=1.5)
