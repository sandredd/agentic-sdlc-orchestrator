from orchestrator.agents.requirements import RequirementsAgent
from orchestrator.contracts import ScenarioKind

from .conftest import state_for


async def test_well_specified_requirement_has_few_ambiguities(provider, node):
    agent = RequirementsAgent(provider)
    statement = (
        "Add custom alias support with 409 on collision, links expire after 30 days via "
        "expires_at, track click_count and last_accessed_at exposed via a stats endpoint, "
        "rate-limit URL creation per client IP, store data in SQLite, no auth for the prototype."
    )
    result = await agent.run(node, state_for(statement))
    nreq = result.context_updates["normalized_requirement"]
    assert len(nreq["ambiguities"]) <= 1
    assert not any(a["blocking"] for a in nreq["ambiguities"])


async def test_sparse_requirement_yields_more_assumptions(provider, node):
    agent = RequirementsAgent(provider)
    result = await agent.run(
        node, state_for("Build a URL shortener with core APIs, analytics, and reliability.")
    )
    nreq = result.context_updates["normalized_requirement"]
    assert len(nreq["ambiguities"]) >= 3


async def test_vague_requirement_is_flagged_blocking(provider, node):
    agent = RequirementsAgent(provider)
    result = await agent.run(node, state_for("Make it better, TBD on details"))
    nreq = result.context_updates["normalized_requirement"]
    blocking = [a for a in nreq["ambiguities"] if a["blocking"]]
    assert blocking
    assert nreq["risk"] == "high"
    assert any(f.severity.value == "high" for f in result.findings)


async def test_expiration_synonym_is_recognized_not_flagged(provider, node):
    """Regression test: 'expire' as a bare substring does not match
    'expiration' -- the aspect keyword must be a shared prefix."""
    agent = RequirementsAgent(provider)
    result = await agent.run(
        node, state_for("Support link expiration via an expires_at field.")
    )
    nreq = result.context_updates["normalized_requirement"]
    assert not any("expiration time" in a["question"] for a in nreq["ambiguities"])


async def test_decisions_are_recorded_with_rationale(provider, node):
    agent = RequirementsAgent(provider)
    result = await agent.run(node, state_for("Build a URL shortener."))
    assert result.decisions
    assert all(d.rationale.strip() for d in result.decisions)


async def test_artifact_is_written_and_reflects_ambiguities(provider, node):
    agent = RequirementsAgent(provider)
    result = await agent.run(node, state_for("Build a URL shortener."))
    doc = result.artifacts[0].content
    assert "Ambiguities" in doc


async def test_model_backed_path_is_used_when_provider_returns_valid_json(node):
    class FakeProvider:
        async def complete(self, **kw):
            import json

            return json.dumps(
                {
                    "problem_statement": "custom problem",
                    "in_scope": ["a"],
                    "out_of_scope": [],
                    "functional": ["f1"],
                    "non_functional": [],
                    "acceptance": [{"statement": "works", "verifiable_by": "unit"}],
                    "ambiguities": [],
                    "risk": "low",
                }
            )

    agent = RequirementsAgent(FakeProvider())
    result = await agent.run(node, state_for("anything"))
    nreq = result.context_updates["normalized_requirement"]
    assert nreq["problem_statement"] == "custom problem"
    assert nreq["risk"] == "low"


async def test_brownfield_kind_is_reflected_in_problem_statement(provider, node):
    agent = RequirementsAgent(provider)
    result = await agent.run(node, state_for("Fix the redirect bug.", ScenarioKind.BROWNFIELD))
    nreq = result.context_updates["normalized_requirement"]
    assert "brownfield" in nreq["problem_statement"]
