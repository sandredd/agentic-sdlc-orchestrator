from orchestrator.agents.docs import DocsAgent

from .conftest import state_for


async def test_readme_only_documents_enabled_capabilities(provider, node):
    state = state_for("Build a URL shortener.")
    state.context.set(
        "code", {"capabilities": {"stats": False, "alias": True}}, writer="implementation"
    )
    result = await DocsAgent(provider).run(node, state)
    readme = result.artifacts[0].content
    assert "/api/urls/{code}/stats" not in readme


async def test_readme_documents_stats_when_enabled(provider, node):
    state = state_for("Build a URL shortener.")
    state.context.set("code", {"capabilities": {"stats": True}}, writer="implementation")
    result = await DocsAgent(provider).run(node, state)
    readme = result.artifacts[0].content
    assert "/api/urls/{code}/stats" in readme
