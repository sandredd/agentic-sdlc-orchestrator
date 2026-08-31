from orchestrator.agents.architecture import ArchitectureAgent
from orchestrator.agents.requirements import RequirementsAgent
from orchestrator.contracts import ScenarioKind
from orchestrator.core.workspace import Workspace

from .conftest import state_for


async def test_greenfield_produces_api_spec_and_decisions(provider, node, tmp_path):
    state = state_for("Build a URL shortener.")
    req_result = await RequirementsAgent(provider).run(node, state)
    state.absorb(req_result, writer="requirements")

    result = await ArchitectureAgent(provider).run(node, state)
    paths = {a.path for a in result.artifacts}
    assert "api/openapi.yaml" in paths
    assert "docs/architecture.md" in paths
    assert result.decisions


async def test_brownfield_reasons_over_seeded_workspace(provider, node, tmp_path):
    seed = tmp_path / "seed"
    (seed / "app").mkdir(parents=True)
    (seed / "app" / "redirect_handler.py").write_text(
        "def redirect(code):\n    return lookup(code)\n"
    )
    (seed / "app" / "billing.py").write_text("def charge(user): ...\n")
    ws = Workspace(tmp_path / "ws", seed_from=seed)

    state = state_for(
        "Fix a bug in the redirect handler for expired links.", ScenarioKind.BROWNFIELD
    )
    req_result = await RequirementsAgent(provider).run(node, state)
    state.absorb(req_result, writer="requirements")

    result = await ArchitectureAgent(provider, workspace=ws).run(node, state)
    impacted = result.decisions[0].choice
    assert "redirect_handler.py" in impacted
    assert "billing.py" not in impacted


async def test_brownfield_with_no_workspace_reports_gap_not_crash(provider, node):
    state = state_for("Fix the bug.", ScenarioKind.BROWNFIELD)
    req_result = await RequirementsAgent(provider).run(node, state)
    state.absorb(req_result, writer="requirements")

    result = await ArchitectureAgent(provider).run(node, state)
    assert result.decisions[0].confidence < 0.5
