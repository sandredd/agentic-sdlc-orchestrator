from orchestrator.agents.planning import PlanningAgent
from orchestrator.agents.requirements import RequirementsAgent

from .conftest import state_for


async def _plan_for(provider, node, statement):
    state = state_for(statement)
    req_result = await RequirementsAgent(provider).run(node, state)
    state.absorb(req_result, writer="requirements")
    return await PlanningAgent(provider).run(node, state)


async def test_core_tasks_always_present(provider, node):
    result = await _plan_for(provider, node, "Build a URL shortener.")
    titles = {t["title"] for t in result.context_updates["plan"]["tasks"]}
    assert "data model and storage repository" in titles
    assert "create endpoint" in titles
    assert "redirect endpoint" in titles


async def test_optional_tasks_gated_by_functional_requirements(provider, node):
    result = await _plan_for(
        provider, node,
        "Build a URL shortener with custom aliases, expiration, click analytics, "
        "and rate limiting.",
    )
    titles = {t["title"] for t in result.context_updates["plan"]["tasks"]}
    assert {"custom alias handling", "expiration handling", "stats endpoint",
            "rate limiting middleware"} <= titles


async def test_dependencies_reference_real_task_ids(provider, node):
    result = await _plan_for(provider, node, "Build a URL shortener with custom aliases.")
    tasks = result.context_updates["plan"]["tasks"]
    ids = {t["id"] for t in tasks}
    for t in tasks:
        for dep in t["depends_on"]:
            assert dep in ids


async def test_storage_task_has_no_dependencies_and_is_first_in_topology(provider, node):
    result = await _plan_for(provider, node, "Build a URL shortener.")
    tasks = result.context_updates["plan"]["tasks"]
    storage = next(t for t in tasks if t["title"] == "data model and storage repository")
    assert storage["depends_on"] == []


async def test_rationale_and_json_artifact_are_produced(provider, node):
    result = await _plan_for(provider, node, "Build a URL shortener.")
    assert result.decisions[0].rationale
    paths = {a.path for a in result.artifacts}
    assert "docs/plan.json" in paths
    assert "docs/plan.md" in paths
