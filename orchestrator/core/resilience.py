"""Bounded retry, fallback and cross-stage rollback.

Three distinct mechanisms, deliberately not conflated:

* **Retry** answers "is it worth trying the same thing again" — bounded by
  :class:`~orchestrator.config.RetryPolicy` and gated by
  :class:`FailureClass` so a permanent failure (a bad schema) is never
  retried into a timeout.
* **Fallback** answers "if retries are exhausted, is there a degraded path
  that still produces something reviewable" — e.g. a stricter code-generation
  strategy falling back to a simpler, more conservative one.
* **Rollback** answers "given this stage failed for good, what upstream state
  does it invalidate" — which can span *multiple* stages via
  :attr:`StageNode.rollback_with`, not just the failing stage's own workspace
  snapshot (the engine already handles that single-stage case).

All three emit their own ledger events, because "retried three times, fell
back, and the fallback still needed a partial rollback" is exactly the kind of
sequence an audit-grade trail has to make legible.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from orchestrator.config import RetryPolicy
from orchestrator.contracts import StageResult

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState


class FailureClass(StrEnum):
    """How a failure should be treated by the retry loop."""

    TRANSIENT = "transient"      # retry as-is: network blip, provider 5xx
    RATE_LIMITED = "rate_limited"  # retry with a longer, explicit backoff
    PERMANENT = "permanent"      # retrying will not help: bad input, logic error
    POLICY = "policy"            # blocked by a guardrail: retrying is unsafe


_TRANSIENT_MARKERS = ("timeout", "timed out", "connection", "temporarily", "unavailable")
_RATE_LIMIT_MARKERS = ("rate limit", "429", "too many requests", "quota")


def classify(error: BaseException | str) -> FailureClass:
    """Best-effort classification from an exception or error string.

    A dedicated exception type is preferred where the caller has one; this is
    the fallback for the common case of a bare ``RuntimeError`` or provider
    error string, so classification degrades gracefully instead of defaulting
    every unknown failure to endless retry.
    """
    text = str(error).lower()
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return FailureClass.RATE_LIMITED
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return FailureClass.TRANSIENT
    if isinstance(error, TimeoutError | asyncio.TimeoutError):
        return FailureClass.TRANSIENT
    return FailureClass.PERMANENT


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    delay_seconds: float
    reason: str

    def __bool__(self) -> bool:
        return self.should_retry


class RetryController:
    """Wraps :class:`RetryPolicy` with failure-class awareness.

    A node may override the global budget via
    :attr:`StageNode.max_attempts`; :class:`FailureClass.POLICY` never gets a
    retry regardless of budget, because a guardrail violation will not become
    less true on attempt two.
    """

    def __init__(self, default_policy: RetryPolicy) -> None:
        self.default_policy = default_policy

    def decide(
        self, node: StageNode, attempt: int, failure: FailureClass
    ) -> RetryDecision:
        max_attempts = node.max_attempts or self.default_policy.max_attempts

        if failure is FailureClass.POLICY:
            return RetryDecision(False, 0.0, "policy violations are not retried")
        if failure is FailureClass.PERMANENT:
            return RetryDecision(False, 0.0, "permanent failure: retrying will not help")
        if attempt >= max_attempts:
            return RetryDecision(
                False, 0.0, f"exhausted retry budget ({max_attempts} attempt(s))"
            )

        delay = self.default_policy.delay_for(attempt + 1)
        if failure is FailureClass.RATE_LIMITED:
            delay = max(delay, self.default_policy.max_backoff_seconds)
        return RetryDecision(True, delay, f"{failure.value} failure, attempt {attempt + 1} next")


class FallbackStrategy(ABC):
    """A degraded execution path tried once the retry budget is exhausted.

    A fallback receives the *same* inputs as the primary path and must return
    a :class:`StageResult` exactly like any executor — the engine cannot tell
    the difference except that :attr:`StageState.fallback_used` gets set,
    which is what a reviewer sees in the reliability report.
    """

    name: str = "fallback"
    description: str = ""

    @abstractmethod
    async def execute(self, node: StageNode, state: RunState) -> StageResult: ...


class ConservativeFallback(FallbackStrategy):
    """Wraps a simpler, lower-risk executor. Used, for example, to fall back
    from an LLM-authored implementation to a template-based one that is less
    capable but far less likely to fail again the same way."""

    name = "conservative"

    def __init__(self, executor, *, description: str = "") -> None:
        self._executor = executor
        self.description = description or "simplified, lower-risk execution path"

    async def execute(self, node: StageNode, state: RunState) -> StageResult:
        result = await self._executor(node, state)
        note = f"[fallback:{self.name}] {result.summary}".strip()
        return result.model_copy(update={"summary": note})


class SkipWithFindingFallback(FallbackStrategy):
    """No safe degraded path exists; produce a findings-only result so the
    stage still exits cleanly and the gap is visible in the run report rather
    than silently absent."""

    name = "skip_with_finding"
    description = "record the gap and let the run continue without this stage's output"

    async def execute(self, node: StageNode, state: RunState) -> StageResult:
        from orchestrator.contracts import Finding, Severity

        return StageResult(
            stage=node.name,
            summary=f"{node.title} could not complete; proceeding without its output",
            findings=(
                Finding(
                    severity=Severity.MEDIUM,
                    category="reliability",
                    summary=f"{node.name} exhausted retries and had no viable fallback",
                    raised_by="engine:fallback",
                    remediation="review manually and re-run this stage",
                ),
            ),
        )


@dataclass(frozen=True)
class RollbackPlan:
    """The stages a rollback must revert, computed from
    :attr:`StageNode.rollback_with` closure plus everything downstream of the
    failing stage that already ran — since their inputs are about to become
    invalid."""

    trigger: str
    stages: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.stages)


def plan_rollback(graph, failing_stage: str, ran: set[str]) -> RollbackPlan:
    """Compute which already-executed stages must be rolled back alongside
    ``failing_stage``.

    Two sources feed the set: the node's explicit ``rollback_with`` closure
    (for tightly coupled stages, e.g. a schema migration and its seed data),
    and every descendant of the failing stage that has already run and
    therefore consumed output that is about to be invalidated.
    """
    explicit: set[str] = set()
    frontier = {failing_stage}
    while frontier:
        next_frontier: set[str] = set()
        for name in frontier:
            for coupled in graph[name].rollback_with:
                if coupled not in explicit and coupled != failing_stage:
                    explicit.add(coupled)
                    next_frontier.add(coupled)
        frontier = next_frontier

    # Only stages that actually ran have a workspace snapshot to restore; an
    # explicitly coupled stage that never got that far needs no rollback.
    downstream_ran = graph.descendants(failing_stage) & ran
    stages = tuple(sorted((explicit | downstream_ran) & ran))
    return RollbackPlan(trigger=failing_stage, stages=stages)
