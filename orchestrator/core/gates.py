"""Entry and exit gates.

A gate is a named, auditable predicate on the run. Entry gates answer "is this
stage allowed to start"; exit gates answer "is this output admissible". Both
emit a :class:`GateDecision` that lands in the ledger, so a reviewer can see
not just *that* a stage passed but *which* checks it passed and why.

Gates are objects rather than inline `if` statements for three reasons: they
are individually unit-testable, they are reusable across stages, and they have
stable names that can be referenced in an audit report.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestrator.contracts import RiskLevel, Severity, StageOutcome, StageResult

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    gate: str
    reason: str
    remediation: str | None = None

    def __bool__(self) -> bool:
        return self.passed

    @classmethod
    def allow(cls, gate: str, reason: str = "") -> GateDecision:
        return cls(passed=True, gate=gate, reason=reason or "precondition met")

    @classmethod
    def block(cls, gate: str, reason: str, remediation: str | None = None) -> GateDecision:
        return cls(passed=False, gate=gate, reason=reason, remediation=remediation)


class Gate:
    """Base for both gate flavours: a stable ``name`` is what makes a gate
    citable in an audit report."""

    name: str = "gate"
    description: str = ""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"


class EntryGate(Gate, ABC):
    """Precondition checked before a stage is dispatched."""

    @abstractmethod
    def check(self, node: StageNode, state: RunState) -> GateDecision: ...


class ExitGate(Gate, ABC):
    """Postcondition checked against a stage's result.

    A failed exit gate means the result is inadmissible: it is not folded into
    run state, and the stage is eligible for retry. This is the mechanism that
    keeps a plausible-looking but invalid agent output from contaminating
    everything downstream.
    """

    @abstractmethod
    def check(self, node: StageNode, state: RunState, result: StageResult) -> GateDecision: ...


# --------------------------------------------------------------------------
# Built-in entry gates
# --------------------------------------------------------------------------


class RequiredContextGate(EntryGate):
    """The stage's declared ``consumes`` keys must actually be present.

    The graph proves statically that *some* ancestor produces each key; this
    proves at runtime that it really did.
    """

    name = "entry.required_context"
    description = "declared input context keys are present"

    def check(self, node: StageNode, state: RunState) -> GateDecision:
        missing = [key for key in node.consumes if not state.context.has(key)]
        if missing:
            return GateDecision.block(
                self.name,
                f"missing required context: {', '.join(sorted(missing))}",
                remediation="re-run the upstream stage that produces these keys",
            )
        return GateDecision.allow(self.name, f"{len(node.consumes)} input key(s) present")


class NoBlockingAmbiguityGate(EntryGate):
    """Refuse to build on top of an unresolved, blocking ambiguity.

    This is the safety valve for the ambiguous scenario: the system is allowed
    to proceed on a *recorded assumption*, but not past a question a human
    marked as genuinely blocking.
    """

    name = "entry.no_blocking_ambiguity"
    description = "no unresolved blocking ambiguity remains"

    def check(self, node: StageNode, state: RunState) -> GateDecision:
        nreq = state.normalized
        if nreq is None:
            return GateDecision.allow(self.name, "no normalized requirement yet")
        blocking = nreq.blocking_ambiguities
        if blocking:
            questions = "; ".join(a.question for a in blocking)
            return GateDecision.block(
                self.name,
                f"{len(blocking)} blocking ambiguity/ies unresolved: {questions}",
                remediation="resolve with a human answer, or downgrade to a recorded assumption",
            )
        return GateDecision.allow(self.name, "no blocking ambiguities")


class MaxRiskGate(EntryGate):
    """Hold a stage whose upstream risk exceeds a ceiling."""

    name = "entry.max_risk"

    def __init__(self, ceiling: RiskLevel = RiskLevel.HIGH) -> None:
        self.ceiling = ceiling
        self.description = f"normalized risk is at or below {ceiling.value}"

    def check(self, node: StageNode, state: RunState) -> GateDecision:
        nreq = state.normalized
        if nreq is None:
            return GateDecision.allow(self.name, "risk not yet assessed")
        if nreq.risk.rank > self.ceiling.rank:
            return GateDecision.block(
                self.name,
                f"risk {nreq.risk.value} exceeds ceiling {self.ceiling.value}",
                remediation="reduce scope, or raise the ceiling with explicit human sign-off",
            )
        return GateDecision.allow(self.name, f"risk {nreq.risk.value} within ceiling")


class UpstreamCleanGate(EntryGate):
    """Named upstream stages must have succeeded outright, not merely been
    skipped. Used where a bypass would be unsafe (e.g. do not release on the
    back of a skipped test stage)."""

    name = "entry.upstream_clean"

    def __init__(self, *stages: str) -> None:
        self.stages = stages
        self.description = f"upstream {', '.join(stages)} succeeded (not skipped)"

    def check(self, node: StageNode, state: RunState) -> GateDecision:
        from orchestrator.core.state import StageStatus

        unclean = [
            s for s in self.stages if state.stage(s).status is not StageStatus.SUCCEEDED
        ]
        if unclean:
            return GateDecision.block(
                self.name,
                f"upstream not cleanly succeeded: {', '.join(unclean)}",
                remediation="re-run the upstream stage rather than bypassing it",
            )
        return GateDecision.allow(self.name, "upstream clean")


# --------------------------------------------------------------------------
# Built-in exit gates
# --------------------------------------------------------------------------


class OutcomeGate(ExitGate):
    """The agent must not have reported failure."""

    name = "exit.outcome"
    description = "stage reported a non-failed outcome"

    def check(self, node: StageNode, state: RunState, result: StageResult) -> GateDecision:
        if result.outcome is StageOutcome.FAILED:
            return GateDecision.block(self.name, result.summary or "stage reported failure")
        return GateDecision.allow(self.name, result.outcome.value)


class PromisedOutputGate(ExitGate):
    """The stage must actually produce every context key it declared.

    Without this, an agent can 'succeed' while quietly producing nothing, and
    the failure surfaces much later as a missing-input error somewhere else.
    """

    name = "exit.promised_output"
    description = "stage produced every context key it declared"

    def check(self, node: StageNode, state: RunState, result: StageResult) -> GateDecision:
        missing = [key for key in node.produces if key not in result.context_updates]
        if missing:
            return GateDecision.block(
                self.name,
                f"declared but did not produce: {', '.join(sorted(missing))}",
                remediation="retry the stage; if it recurs the agent contract is wrong",
            )
        return GateDecision.allow(self.name, f"{len(node.produces)} output key(s) produced")


class SeverityGate(ExitGate):
    """Reject a result carrying findings above a severity ceiling."""

    name = "exit.severity"

    def __init__(self, ceiling: Severity = Severity.HIGH) -> None:
        self.ceiling = ceiling
        self.description = f"no finding above {ceiling.value}"

    def check(self, node: StageNode, state: RunState, result: StageResult) -> GateDecision:
        offenders = [f for f in result.findings if f.severity.rank > self.ceiling.rank]
        if offenders:
            top = max(offenders, key=lambda f: f.severity.rank)
            return GateDecision.block(
                self.name,
                f"{len(offenders)} finding(s) above {self.ceiling.value}; "
                f"worst: [{top.severity.value}] {top.summary}",
                remediation=top.remediation or "remediate the finding and re-run the stage",
            )
        return GateDecision.allow(
            self.name, f"{len(result.findings)} finding(s), all within ceiling"
        )


class ArtifactsProducedGate(ExitGate):
    """A stage whose job is to produce artifacts must produce some."""

    name = "exit.artifacts_produced"

    def __init__(self, minimum: int = 1, kinds: tuple[str, ...] = ()) -> None:
        self.minimum = minimum
        self.kinds = kinds
        self.description = f"produced at least {minimum} artifact(s)"

    def check(self, node: StageNode, state: RunState, result: StageResult) -> GateDecision:
        artifacts = result.artifacts
        if self.kinds:
            artifacts = tuple(a for a in artifacts if a.kind.value in self.kinds)
        if len(artifacts) < self.minimum:
            wanted = f" of kind {'/'.join(self.kinds)}" if self.kinds else ""
            return GateDecision.block(
                self.name,
                f"expected >= {self.minimum} artifact(s){wanted}, got {len(artifacts)}",
            )
        return GateDecision.allow(self.name, f"{len(artifacts)} artifact(s)")


class DecisionsRecordedGate(ExitGate):
    """High-judgement stages must record their reasoning.

    Enforcing lineage as a gate rather than a convention is what keeps the
    audit trail complete when an agent is terse.
    """

    name = "exit.decisions_recorded"

    def __init__(self, minimum: int = 1) -> None:
        self.minimum = minimum
        self.description = f"recorded at least {minimum} decision(s) with rationale"

    def check(self, node: StageNode, state: RunState, result: StageResult) -> GateDecision:
        recorded = [d for d in result.decisions if d.rationale.strip()]
        if len(recorded) < self.minimum:
            return GateDecision.block(
                self.name,
                f"expected >= {self.minimum} decision(s) with rationale, got {len(recorded)}",
                remediation="record the choice, its alternatives and why it was made",
            )
        return GateDecision.allow(self.name, f"{len(recorded)} decision(s) recorded")
