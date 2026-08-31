from orchestrator.agents.implementation import ImplementationAgent
from orchestrator.agents.testing import TestingAgent
from orchestrator.core.workspace import Workspace

from .conftest import state_for


async def _implemented_state(provider, node, tmp_path, task_titles):
    state = state_for("Build a URL shortener.")
    state.context.set("plan", {"tasks": [{"title": t} for t in task_titles]}, writer="planning")
    ws = Workspace(tmp_path / "ws")
    impl_result = await ImplementationAgent(provider).run(node, state)
    for a in impl_result.artifacts:
        ws.write_artifact(a)
    state.absorb(impl_result, writer="implementation")
    return state, ws


async def test_generated_suite_passes_against_generated_code(provider, node, tmp_path):
    state, ws = await _implemented_state(
        provider, node, tmp_path,
        ["data model and storage repository", "create endpoint", "redirect endpoint",
         "custom alias handling", "stats endpoint"],
    )
    result = await TestingAgent(provider, workspace=ws).run(node, state)
    assert result.context_updates["test_report"]["ran"] is True
    assert result.context_updates["test_report"]["failed"] == 0
    assert result.context_updates["test_report"]["passed"] > 0
    assert not result.findings


async def test_a_real_regression_is_detected(provider, node, tmp_path):
    state, ws = await _implemented_state(
        provider, node, tmp_path, ["data model and storage repository", "create endpoint"]
    )
    broken = ws.read("app/routes.py").replace("status_code=302", "status_code=200")
    assert broken != ws.read("app/routes.py")
    ws.write("app/routes.py", broken)

    result = await TestingAgent(provider, workspace=ws).run(node, state)
    assert result.context_updates["test_report"]["failed"] >= 1
    assert result.findings
    assert result.findings[0].severity.value in {"high", "blocker"}


async def test_no_workspace_reports_gracefully_not_crash(provider, node):
    state = state_for("Build a URL shortener.")
    state.context.set("code", {"capabilities": {}}, writer="implementation")
    result = await TestingAgent(provider).run(node, state)
    assert result.context_updates["test_report"]["ran"] is False
    assert result.findings
    assert result.findings[0].severity.value == "medium"
