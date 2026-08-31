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
