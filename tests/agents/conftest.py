import contextlib
import sys

import pytest

from orchestrator.contracts import Requirement, ScenarioKind
from orchestrator.core.graph import StageNode
from orchestrator.core.state import RunState
from orchestrator.providers.deterministic import DeterministicProvider


@pytest.fixture
def provider():
    return DeterministicProvider()


@pytest.fixture
def node():
    return StageNode(name="x", title="X")


def requirement(statement: str, kind: ScenarioKind = ScenarioKind.GREENFIELD) -> Requirement:
    return Requirement(title="t", statement=statement, kind=kind)


def state_for(statement: str, kind: ScenarioKind = ScenarioKind.GREENFIELD) -> RunState:
    return RunState(run_id="r", requirement=requirement(statement, kind))


@contextlib.contextmanager
def materialized_app(ws_root, tmp_path, monkeypatch):
    """Import a generated `app.main` module from a materialized workspace as
    a real package, on an isolated SQLite file.

    `app/config.py`'s DATABASE_PATH defaults to the *relative* path
    "shortener.db" -- relative to the process's current working directory,
    not to the generated app's own directory. Two tests in the same pytest
    run, each materializing a different generated app into a different
    tmp_path, would otherwise silently open the *same* `shortener.db` file
    in the shared CWD: whichever ran `init_db()` first fixes the schema
    (CREATE TABLE IF NOT EXISTS is a no-op after that), and a later test
    whose generated code expects a column the first schema doesn't have
    (e.g. `expires_at`) fails with a confusing OperationalError that looks
    like a product bug but is purely test cross-contamination. Pointing
    SHORTENER_DB_PATH at a tmp_path-scoped file, via monkeypatch so it
    reverts automatically, is what actually isolates each test.
    """
    monkeypatch.setenv("SHORTENER_DB_PATH", str(tmp_path / "test.db"))
    sys.path.insert(0, str(ws_root))
    for mod in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]
    try:
        import importlib

        main = importlib.import_module("app.main")
        yield main.app
    finally:
        sys.path.remove(str(ws_root))
        for mod in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
            del sys.modules[mod]
