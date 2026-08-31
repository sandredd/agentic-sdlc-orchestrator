"""Shared agent scaffolding.

An agent is anything matching `orchestrator.core.engine.StageExecutor`: an
async callable of `(StageNode, RunState) -> StageResult`. That is the entire
contract the engine cares about. `Agent` adds the parts every concrete stage
agent needs regardless of what it does -- a provider it can ask for help, and
constructors for the contract objects (`StageResult`, `Artifact`, `Decision`,
`Finding`) so a subclass's `run()` reads as engineering logic, not
boilerplate.

The central design choice is :meth:`Agent.think`: it asks the provider for a
JSON object and returns `None` on *any* failure -- no provider configured, a
network error, or a response that is not valid JSON. Callers never branch on
which of those happened; they branch on "did I get useful structure back",
and fall back to their own deterministic heuristic when they didn't. That is
what lets every agent run identically, just with different fidelity, whether
or not a real model is behind the provider.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from orchestrator.contracts import (
    Artifact,
    ArtifactKind,
    Decision,
    Finding,
    RiskLevel,
    Severity,
    StageOutcome,
    StageResult,
)
from orchestrator.providers.base import Provider, ProviderError

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState
    from orchestrator.core.workspace import Workspace

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | list[Any] | None:
    """Best-effort JSON extraction from a model response: strip a markdown
    fence if present, otherwise take the widest {...} or [...] span. Models
    reliably wrap JSON in prose or fences even when asked not to."""
    candidate = text.strip()
    if fenced := _FENCE.search(candidate):
        candidate = fenced.group(1).strip()

    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


class Agent(ABC):
    """Base for a single SDLC stage's execution logic."""

    stage_name: str = "agent"

    def __init__(self, provider: Provider, *, workspace: Workspace | None = None) -> None:
        self.provider = provider
        # Read-only access to whatever the run's workspace was seeded with --
        # e.g. an existing codebase in a brownfield run. Agents write outputs
        # by returning Artifacts in their StageResult, never by writing to the
        # workspace directly, so this is read-only by convention, not by type.
        self.workspace = workspace

    async def __call__(self, node: StageNode, state: RunState) -> StageResult:
        return await self.run(node, state)

    @abstractmethod
    async def run(self, node: StageNode, state: RunState) -> StageResult: ...

    # -- provider access -----------------------------------------------

    async def think(
        self, *, system: str, prompt: str, max_tokens: int = 1536
    ) -> dict[str, Any] | list[Any] | None:
        try:
            text = await self.provider.complete(
                system=system, prompt=prompt, max_tokens=max_tokens
            )
        except ProviderError:
            return None
        return _extract_json(text)

    # -- contract builders -----------------------------------------------

    def result(
        self,
        *,
        summary: str,
        artifacts: tuple[Artifact, ...] = (),
        findings: tuple[Finding, ...] = (),
        decisions: tuple[Decision, ...] = (),
        context: dict[str, Any] | None = None,
        outcome: StageOutcome = StageOutcome.SUCCEEDED,
        replan_reason: str | None = None,
        metrics: dict[str, float] | None = None,
    ) -> StageResult:
        return StageResult(
            stage=self.stage_name,
            outcome=outcome,
            summary=summary,
            artifacts=artifacts,
            findings=findings,
            decisions=decisions,
            context_updates=context or {},
            replan_reason=replan_reason,
            metrics=metrics or {},
        )

    def artifact(self, path: str, kind: ArtifactKind, content: str) -> Artifact:
        if not content.endswith("\n"):
            content += "\n"
        return Artifact(path=path, kind=kind, content=content, produced_by=self.stage_name)

    def decision(
        self,
        question: str,
        choice: str,
        rationale: str,
        *,
        alternatives: tuple[str, ...] = (),
        confidence: float = 0.8,
        derived_from: tuple[str, ...] = (),
    ) -> Decision:
        return Decision(
            stage=self.stage_name,
            question=question,
            choice=choice,
            rationale=rationale,
            alternatives=alternatives,
            made_by=f"agent:{self.stage_name}",
            confidence=confidence,
            derived_from=derived_from,
        )

    def finding(
        self,
        severity: Severity,
        category: str,
        summary: str,
        *,
        detail: str = "",
        path: str | None = None,
        remediation: str | None = None,
    ) -> Finding:
        return Finding(
            severity=severity,
            category=category,
            summary=summary,
            detail=detail,
            path=path,
            raised_by=self.stage_name,
            remediation=remediation,
        )


__all__ = ["Agent", "RiskLevel", "Severity", "StageOutcome"]
