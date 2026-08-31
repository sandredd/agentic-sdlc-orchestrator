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

from .conftest import state_for

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
