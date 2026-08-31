"""Domain contracts shared by the engine, the agents and the audit trail.

Everything that crosses a stage boundary is one of these models. Keeping the
vocabulary in a single module is what makes cross-stage context transfer and
decision lineage auditable rather than ad-hoc dictionary passing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Frozen(BaseModel):
    """Immutable value object. Stage outputs are never mutated in place; a new
    revision is produced instead, so lineage stays intact."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Mutable(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Requirements
# --------------------------------------------------------------------------


class ScenarioKind(StrEnum):
    GREENFIELD = "greenfield"
    BROWNFIELD = "brownfield"
    AMBIGUOUS = "ambiguous"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2, "critical": 3}[self.value]


class Requirement(Frozen):
    """The raw ask, as handed to the system."""

    id: str = Field(default_factory=lambda: new_id("req"))
    title: str
    statement: str
    kind: ScenarioKind
    submitted_by: str = "human"
    submitted_at: datetime = Field(default_factory=utcnow)
    constraints: tuple[str, ...] = ()
    acceptance_hints: tuple[str, ...] = ()


class Ambiguity(Frozen):
    """A gap the requirements agent could not resolve from the statement alone."""

    id: str = Field(default_factory=lambda: new_id("amb"))
    question: str
    why_it_matters: str
    options: tuple[str, ...] = ()
    assumption: str
    confidence: float = Field(ge=0.0, le=1.0)
    blocking: bool = False


class AcceptanceCriterion(Frozen):
    id: str = Field(default_factory=lambda: new_id("ac"))
    statement: str
    verifiable_by: Literal["unit", "integration", "manual", "static"] = "unit"


class NormalizedRequirement(Frozen):
    """The engineering problem, normalized. This is the artifact every
    downstream stage reads instead of re-interpreting the raw prose."""

    id: str = Field(default_factory=lambda: new_id("nreq"))
    source_requirement_id: str
    problem_statement: str
    in_scope: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    functional: tuple[str, ...]
    non_functional: tuple[str, ...]
    acceptance: tuple[AcceptanceCriterion, ...]
    ambiguities: tuple[Ambiguity, ...] = ()
    assumptions: tuple[str, ...] = ()
    risk: RiskLevel = RiskLevel.MEDIUM

    @property
    def blocking_ambiguities(self) -> tuple[Ambiguity, ...]:
        return tuple(a for a in self.ambiguities if a.blocking)


# --------------------------------------------------------------------------
# Plan / tasks
# --------------------------------------------------------------------------


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class Task(Frozen):
    """A unit of decomposed work. `depends_on` gives the planner's own
    intra-stage ordering, independent of the SDLC stage graph."""

    id: str = Field(default_factory=lambda: new_id("task"))
    title: str
    detail: str
    stage: str
    depends_on: tuple[str, ...] = ()
    estimate_points: int = 1
    risk: RiskLevel = RiskLevel.LOW
    touches: tuple[str, ...] = ()


class Plan(Frozen):
    id: str = Field(default_factory=lambda: new_id("plan"))
    normalized_requirement_id: str
    tasks: tuple[Task, ...]
    sequencing_rationale: str
    revision: int = 1


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------


class ArtifactKind(StrEnum):
    CODE = "code"
    TEST = "test"
    SCHEMA = "schema"
    API_SPEC = "api_spec"
    DOC = "doc"
    REPORT = "report"
    CONFIG = "config"


class Artifact(Frozen):
    """A concrete engineering output written into the run workspace.

    `content_hash` is what the ledger chains over, so a reviewer can prove the
    artifact they are looking at is the one the gate approved.
    """

    id: str = Field(default_factory=lambda: new_id("art"))
    path: str
    kind: ArtifactKind
    content: str
    content_hash: str = ""
    produced_by: str = ""
    supersedes: str | None = None

    def with_hash(self) -> Artifact:
        import hashlib

        digest = hashlib.sha256(self.content.encode()).hexdigest()
        return self.model_copy(update={"content_hash": digest})


# --------------------------------------------------------------------------
# Findings / validation
# --------------------------------------------------------------------------


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKER = "blocker"

    @property
    def rank(self) -> int:
        return {"info": 0, "low": 1, "medium": 2, "high": 3, "blocker": 4}[self.value]


class Finding(Frozen):
    """Anything a validating stage wants to raise: a bug, a policy violation,
    a missing test, a trade-off worth recording."""

    id: str = Field(default_factory=lambda: new_id("find"))
    severity: Severity
    category: str
    summary: str
    detail: str = ""
    path: str | None = None
    raised_by: str = ""
    remediation: str | None = None


# --------------------------------------------------------------------------
# Decisions / lineage
# --------------------------------------------------------------------------


class Decision(Frozen):
    """A recorded judgement call. The chain of these across a run *is* the
    decision lineage the assessment asks for: every non-obvious choice carries
    its alternatives and the evidence that justified it."""

    id: str = Field(default_factory=lambda: new_id("dec"))
    stage: str
    question: str
    choice: str
    rationale: str
    alternatives: tuple[str, ...] = ()
    made_by: str = "agent"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    derived_from: tuple[str, ...] = ()
    at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------
# Stage I/O
# --------------------------------------------------------------------------


class StageOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_REPLAN = "needs_replan"


class StageResult(Frozen):
    """What an agent hands back to the engine.

    An agent never mutates run state directly; it returns this and the engine
    decides what is admissible. That separation is the autonomy boundary.
    """

    stage: str
    outcome: StageOutcome = StageOutcome.SUCCEEDED
    summary: str = ""
    artifacts: tuple[Artifact, ...] = ()
    findings: tuple[Finding, ...] = ()
    decisions: tuple[Decision, ...] = ()
    tasks: tuple[Task, ...] = ()
    context_updates: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    replan_reason: str | None = None

    @property
    def blockers(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.BLOCKER)

    def max_severity(self) -> Severity:
        if not self.findings:
            return Severity.INFO
        return max(self.findings, key=lambda f: f.severity.rank).severity
