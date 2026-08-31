from orchestrator.agents.release import ReleaseAgent
from orchestrator.contracts import Artifact, ArtifactKind, Decision, Finding, Severity

from .conftest import state_for


async def test_clean_run_recommends_release(provider, node):
    state = state_for("Build a URL shortener.")
    state.decisions.append(
        Decision(stage="requirements", question="q", choice="c", rationale="r")
    )
    result = await ReleaseAgent(provider).run(node, state)
    doc = result.artifacts[0].content
    assert "Recommended for human approval and release" in doc


async def test_blocker_finding_prevents_release_recommendation(provider, node):
    state = state_for("Build a URL shortener.")
    state.findings.append(
        Finding(severity=Severity.BLOCKER, category="security", summary="critical issue")
    )
    result = await ReleaseAgent(provider).run(node, state)
    doc = result.artifacts[0].content
    assert "DO NOT RELEASE" in doc
    assert "critical issue" in doc


async def test_summary_lists_all_accumulated_artifacts(provider, node):
    state = state_for("Build a URL shortener.")
    state.artifacts["app/main.py"] = Artifact(
        path="app/main.py", kind=ArtifactKind.CODE, content="x", produced_by="implementation"
    ).with_hash()
    result = await ReleaseAgent(provider).run(node, state)
    assert "app/main.py" in result.artifacts[0].content
