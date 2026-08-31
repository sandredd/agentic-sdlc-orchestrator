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


async def test_readme_test_instructions_install_httpx2(provider, node):
    """Regression test: newer starlette's TestClient prefers httpx2 and only
    falls back to httpx (with a deprecation warning) if httpx2 is absent --
    the README used to say `pip install pytest httpx`, which still works via
    that fallback today but is one starlette release away from breaking."""
    state = state_for("Build a URL shortener.")
    state.context.set("code", {"capabilities": {}}, writer="implementation")
    result = await DocsAgent(provider).run(node, state)
    readme = result.artifacts[0].content
    assert "pip install pytest httpx2 httpx" in readme
