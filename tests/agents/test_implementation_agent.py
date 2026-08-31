import ast
import itertools

import pytest

from orchestrator.agents.implementation import (
    ImplementationAgent,
    _codec_py,
    _config_py,
    _main_py,
    _middleware_py,
    _models_py,
    _routes_py,
    _storage_py,
)

from .conftest import materialized_app, state_for

ALL_CAPS = [
    dict(zip(["alias", "expiry", "stats", "rate_limit"], combo, strict=True))
    for combo in itertools.product([False, True], repeat=4)
]


@pytest.mark.parametrize("caps", ALL_CAPS)
def test_every_capability_combination_produces_valid_python(caps):
    files = [_config_py(), _codec_py(), _storage_py(caps), _models_py(caps), _routes_py(caps)]
    if caps["rate_limit"]:
        files.append(_middleware_py())
    files.append(_main_py(caps))
    for content in files:
        ast.parse(content)  # raises SyntaxError on failure


def test_codec_roundtrips_and_is_monotonic_length_stable():
    from orchestrator.agents.implementation import _codec_py

    ns: dict = {}
    exec(compile(_codec_py(), "<codec>", "exec"), ns)
    encode, decode = ns["encode"], ns["decode"]
    for n in (0, 1, 61, 62, 1000, 999999):
        assert decode(encode(n)) == n
    assert len(encode(0)) == len(encode(1)) == 6


async def test_agent_gates_optional_files_on_plan(provider, node):
    state = state_for("Build a URL shortener.")
    state.context.set(
        "plan",
        {"tasks": [{"title": "data model and storage repository"}, {"title": "create endpoint"}]},
        writer="planning",
    )
    result = await ImplementationAgent(provider).run(node, state)
    paths = {a.path for a in result.artifacts}
    assert "app/middleware.py" not in paths
    assert result.context_updates["code"]["capabilities"]["rate_limit"] is False


async def test_agent_includes_middleware_when_rate_limit_task_present(provider, node):
    state = state_for("Build a URL shortener.")
    state.context.set(
        "plan", {"tasks": [{"title": "rate limiting middleware"}]}, writer="planning"
    )
    result = await ImplementationAgent(provider).run(node, state)
    paths = {a.path for a in result.artifacts}
    assert "app/middleware.py" in paths


async def test_pyproject_toml_scopes_pytest_rootdir_to_target(provider, node):
    """Regression test: without its own pytest config, `pytest` run from
    inside a standalone copy of the generated service walks up and picks up
    this orchestrator's own pyproject.toml (asyncio_mode and all), which the
    generated service's own test environment has no plugin for -- and which
    was never meant to apply to it in the first place."""
    state = state_for("Build a URL shortener.")
    state.context.set("plan", {"tasks": []}, writer="planning")
    result = await ImplementationAgent(provider).run(node, state)
    by_path = {a.path: a for a in result.artifacts}
    assert "pyproject.toml" in by_path
    assert "[tool.pytest.ini_options]" in by_path["pyproject.toml"].content
    assert "asyncio_mode" not in by_path["pyproject.toml"].content


async def _materialize(provider, node, tmp_path, statement, task_titles):
    """Run ImplementationAgent for real and write every artifact into a
    fresh workspace, ready to be imported as a real `app` package via
    `materialized_app()`."""
    from orchestrator.core.workspace import Workspace

    state = state_for(statement)
    state.context.set(
        "plan", {"tasks": [{"title": t} for t in task_titles]}, writer="planning"
    )
    ws = Workspace(tmp_path / "ws")
    result = await ImplementationAgent(provider).run(node, state)
    for a in result.artifacts:
        ws.write_artifact(a)
    return ws


async def test_startup_prints_every_registered_route_not_just_top_level_ones(
    provider, node, tmp_path, monkeypatch
):
    """Regression test: the route table used to be built by walking
    `app.routes` and filtering to `isinstance(route, APIRoute)`, but a
    router included via `app.include_router(router)` is wrapped in an
    internal container type (not an `APIRoute` instance) in newer FastAPI
    versions -- so every endpoint from `app/routes.py` silently disappeared
    from the printed list, leaving only the one route (`/health`) declared
    directly on `app`. Fixed by reading `app.openapi()["paths"]` instead,
    the same public, stable source that drives the real `/docs` page.

    This has to run the real generated app (not just parse the template) to
    catch the bug -- it only manifests once FastAPI actually processes a real
    `include_router` call.
    """
    import io
    from contextlib import redirect_stdout

    from fastapi.testclient import TestClient

    ws = await _materialize(
        provider, node, tmp_path,
        "Build a URL shortener with custom aliases.", ["custom alias handling"],
    )
    with materialized_app(ws.root, tmp_path, monkeypatch) as app:
        buf = io.StringIO()
        with redirect_stdout(buf), TestClient(app):
            pass
        output = buf.getvalue()

    assert "GET     /health" in output
    assert "POST    /api/urls" in output
    assert "GET     /{code}" in output
    assert "GET     /api/urls/{code}" in output


async def test_home_page_returns_200_and_lists_real_endpoints(
    provider, node, tmp_path, monkeypatch
):
    """GET / used to 404 -- FastAPI has no default route at the root. Users
    following the printed run URL (http://127.0.0.1:8000) into a browser had
    no way to discover the API from there. Verifies against the real running
    app, not just the template string, since the route table it renders
    comes from the same `_route_table()` helper the startup log uses."""
    from fastapi.testclient import TestClient

    ws = await _materialize(
        provider, node, tmp_path,
        "Build a URL shortener with custom aliases.", ["custom alias handling"],
    )
    with materialized_app(ws.root, tmp_path, monkeypatch) as app, TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "/docs" in response.text
    assert "/api/urls" in response.text
    assert "/{code}" in response.text


async def test_expiry_is_validated_at_creation_not_only_at_redirect(
    provider, node, tmp_path, monkeypatch
):
    """Regression test, reported directly: a link created with an
    already-past expires_at (e.g. a stale example date left over from
    testing via /docs) used to be silently accepted at creation and only
    fail with a confusing 'this link has expired' the first time anyone
    clicked it. Also covers the response bug found investigating this:
    expires_at was never actually included in the API response, always
    showing null regardless of what was stored.
    """
    from datetime import UTC, datetime, timedelta

    from fastapi.testclient import TestClient

    ws = await _materialize(
        provider, node, tmp_path,
        "Build a URL shortener with expiration support.", ["expiration handling"],
    )
    with materialized_app(ws.root, tmp_path, monkeypatch) as app, TestClient(app) as client:
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        rejected = client.post(
            "/api/urls", json={"long_url": "https://example.com/a", "expires_at": past}
        )
        assert rejected.status_code == 422, (
            "creating with an already-past expiry must be rejected at the boundary, "
            "not silently accepted and left to fail confusingly at redirect time"
        )

        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        created = client.post(
            "/api/urls", json={"long_url": "https://example.com/b", "expires_at": future}
        )
        assert created.status_code == 201
        assert created.json()["expires_at"] is not None, (
            "expires_at must be reported back, not silently dropped from the response"
        )

        code = created.json()["code"]
        redirected = client.get(f"/{code}", follow_redirects=False)
        assert redirected.status_code == 302


async def test_expires_at_swagger_example_is_roughly_24h_out_not_current_time(
    provider, node, tmp_path, monkeypatch
):
    """The OpenAPI example FastAPI/Swagger UI pre-fills for a bare `datetime`
    field with no example reads as the current instant -- confusing, and
    right at the edge of the create endpoint's own 'must be in the future'
    check. Reported directly. This only changes what /docs shows; an omitted
    expires_at must still mean "never expires", which the second half of
    this test confirms."""
    from datetime import UTC, datetime, timedelta

    from fastapi.testclient import TestClient

    ws = await _materialize(
        provider, node, tmp_path,
        "Build a URL shortener with expiration support.", ["expiration handling"],
    )
    with materialized_app(ws.root, tmp_path, monkeypatch) as app, TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        example = schema["components"]["schemas"]["CreateUrlRequest"]["properties"][
            "expires_at"
        ]["examples"][0]
        example_dt = datetime.fromisoformat(example)
        delta = example_dt - datetime.now(UTC)
        assert timedelta(hours=23) < delta < timedelta(hours=25)

        created = client.post("/api/urls", json={"long_url": "https://example.com"})
        assert created.status_code == 201
        assert created.json()["expires_at"] is None, "omitting it must still mean never-expires"
