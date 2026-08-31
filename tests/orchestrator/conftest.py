"""Shared fixtures for engine tests.

The executors here are deliberately dumb: Phase 2 is about the *engine's*
control flow, so the stage bodies must not be able to influence the outcome
except through the StageResult contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.contracts import (
    Artifact,
    ArtifactKind,
    Decision,
    Requirement,
    ScenarioKind,
    StageResult,
)
from orchestrator.core.engine import Engine
from orchestrator.core.graph import StageGraph, StageNode
from orchestrator.core.ledger import Ledger
from orchestrator.core.state import RunState
from orchestrator.core.workspace import Workspace


def stage(name: str, deps: tuple[str, ...] = (), **kw) -> StageNode:
    return StageNode(name=name, title=name.title(), depends_on=frozenset(deps), **kw)


def requirement(kind: ScenarioKind = ScenarioKind.GREENFIELD) -> Requirement:
    return Requirement(title="Test requirement", statement="Do the thing.", kind=kind)


def fresh_state(kind: ScenarioKind = ScenarioKind.GREENFIELD) -> RunState:
    return RunState(run_id="run_test", requirement=requirement(kind))


def result(
    stage_name: str,
    *,
    artifacts: tuple[Artifact, ...] = (),
    context: dict | None = None,
    **kw,
) -> StageResult:
    return StageResult(
        stage=stage_name,
        summary=f"{stage_name} done",
        artifacts=artifacts,
        context_updates=context or {},
        **kw,
    )


def code_artifact(path: str, content: str = "x = 1\n") -> Artifact:
    return Artifact(path=path, kind=ArtifactKind.CODE, content=content)


def a_decision(stage_name: str) -> Decision:
    return Decision(
        stage=stage_name,
        question="which storage?",
        choice="sqlite",
        rationale="single-node prototype; no ops burden",
        alternatives=("postgres",),
    )


class RecordingExecutor:
    """Executor that records dispatch order and overlap.

    ``concurrent_peak`` is how the tests prove stages genuinely ran in
    parallel rather than merely being scheduled in the same layer.
    """

    def __init__(
        self,
        *,
        delays: dict[str, float] | None = None,
        results: dict[str, StageResult] | None = None,
        raises: dict[str, Exception] | None = None,
        hook: Callable[[str], None] | None = None,
    ) -> None:
        self.delays = delays or {}
        self.results = results or {}
        self.raises = raises or {}
        self.hook = hook
        self.started: list[str] = []
        self.finished: list[str] = []
        # A single ordered timeline of ("start"|"end", stage). Interleaving
        # questions ("did X start before Y finished?") are only answerable on
        # one timeline -- comparing indices across two lists is meaningless.
        self.timeline: list[tuple[str, str]] = []
        self.in_flight = 0
        self.concurrent_peak = 0

    def started_before_end_of(self, stage_a: str, stage_b: str) -> bool:
        return self.timeline.index(("start", stage_a)) < self.timeline.index(("end", stage_b))

    async def __call__(self, node: StageNode, state: RunState) -> StageResult:
        self.started.append(node.name)
        self.timeline.append(("start", node.name))
        self.in_flight += 1
        self.concurrent_peak = max(self.concurrent_peak, self.in_flight)
        try:
            if self.hook:
                self.hook(node.name)
            await asyncio.sleep(self.delays.get(node.name, 0.0))
            if node.name in self.raises:
                raise self.raises[node.name]
            return self.results.get(node.name) or result(node.name)
        finally:
            self.in_flight -= 1
            self.finished.append(node.name)
            self.timeline.append(("end", node.name))


@pytest.fixture
def make_engine(tmp_path):
    def _make(
        graph: StageGraph,
        executor,
        *,
        config: OrchestratorConfig | None = None,
        **overrides,
    ) -> Engine:
        cfg = config or OrchestratorConfig(run_root=tmp_path / "runs", **overrides)
        run_id = "run_test"
        return Engine(
            graph,
            executor,
            config=cfg,
            run_id=run_id,
            workspace=Workspace(tmp_path / "ws"),
            ledger=Ledger(run_id, path=tmp_path / "ledger.jsonl"),
        )

    return _make
