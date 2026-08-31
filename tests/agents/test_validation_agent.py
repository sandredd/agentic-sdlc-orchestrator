from orchestrator.agents.validation import ValidationAgent
from orchestrator.contracts import Finding, Severity

from .conftest import state_for


async def test_ambiguities_become_recorded_risks(provider, node):
    state = state_for("Build a URL shortener.")
    state.context.set(
        "normalized_requirement",
        {
            "source_requirement_id": "req_1",
            "problem_statement": "p",
            "in_scope": [], "out_of_scope": [], "functional": [], "non_functional": [],
            "acceptance": [],
            "ambiguities": [
                {"question": "q?", "why_it_matters": "w", "assumption": "a",
                 "confidence": 0.5, "blocking": False}
            ],
            "assumptions": ["a"],
            "risk": "medium",
        },
        writer="requirements",
    )
    result = await ValidationAgent(provider).run(node, state)
    report = result.artifacts[0].content
    assert "q?" in report


async def test_blocker_finding_yields_do_not_release_recommendation(provider, node):
    state = state_for("Build a URL shortener.")
    state.findings.append(
        Finding(severity=Severity.BLOCKER, category="security", summary="bad thing")
    )
    result = await ValidationAgent(provider).run(node, state)
    assert result.decisions[0].choice.lower().startswith("no")
