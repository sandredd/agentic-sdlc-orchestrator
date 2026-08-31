from orchestrator.agents.security import SecurityAgent
from orchestrator.contracts import Artifact, ArtifactKind

from .conftest import state_for


async def test_flags_missing_rate_limiting_and_missing_auth(provider, node):
    state = state_for("Build a URL shortener.")
    state.context.set("code", {"capabilities": {"rate_limit": False}}, writer="implementation")
    result = await SecurityAgent(provider).run(node, state)
    categories = {f.category for f in result.findings}
    assert "security" in categories
    assert any("rate limiting" in f.summary for f in result.findings)
    assert any("authentication" in f.summary for f in result.findings)


async def test_does_not_flag_rate_limiting_when_enabled(provider, node):
    state = state_for("Build a URL shortener.")
    state.context.set("code", {"capabilities": {"rate_limit": True}}, writer="implementation")
    result = await SecurityAgent(provider).run(node, state)
    assert not any("no rate limiting" in f.summary for f in result.findings)


async def test_reruns_universal_policy_against_accumulated_artifacts(provider, node):
    state = state_for("Build a URL shortener.")
    state.context.set("code", {"capabilities": {}}, writer="implementation")
    state.artifacts["app/leak.py"] = Artifact(
        path="app/leak.py",
        kind=ArtifactKind.CODE,
        content='password = "hunter2-real-secret-value"\n',
    ).with_hash()
    result = await SecurityAgent(provider).run(node, state)
    assert any(f.severity.value == "blocker" for f in result.findings)


async def test_report_artifact_is_produced(provider, node):
    state = state_for("Build a URL shortener.")
    state.context.set("code", {"capabilities": {}}, writer="implementation")
    result = await SecurityAgent(provider).run(node, state)
    assert result.artifacts[0].path == "docs/security_review.md"
