"""Stateful run representation.

The engine is a state machine over :class:`RunState`, not a call stack. That
distinction is what makes execution *stateful and non-linear*: a run can be
persisted mid-flight, resumed, re-planned, or rolled back to an earlier stage,
because nothing important lives in Python frames.

:class:`ContextStore` is the cross-stage context carrier. It records who wrote
each key and who read it, which is what lets the engine compute — rather than
guess — which downstream stages went stale when an upstream output changed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.contracts import (
    Artifact,
    Decision,
    Finding,
    NormalizedRequirement,
    Plan,
    Requirement,
    StageResult,
    utcnow,
)
from orchestrator.core.approvals import ApprovalLog


class StageStatus(StrEnum):
    PENDING = "pending"                      # not yet eligible
    READY = "ready"                          # dependencies satisfied
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"  # exit held for a human
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"                      # optional stage that was bypassed
    BLOCKED = "blocked"                      # can never become ready
    STALE = "stale"                          # upstream changed; must re-run
    ROLLED_BACK = "rolled_back"              # reverted by a cascading rollback

    @property
    def terminal(self) -> bool:
        return self in {
            StageStatus.SUCCEEDED,
            StageStatus.FAILED,
            StageStatus.SKIPPED,
            StageStatus.BLOCKED,
        }

    @property
    def satisfies_dependents(self) -> bool:
        """Whether downstream stages may treat this as a met dependency.

        SKIPPED counts: an optional stage that was deliberately bypassed must
        not deadlock the graph behind it.
        """
        return self in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    HALTED = "halted"

    @property
    def terminal(self) -> bool:
        return self in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.HALTED}


class HaltReason(StrEnum):
    DEADLINE_EXCEEDED = "deadline_exceeded"
    BLOCKING_FAILURE = "blocking_failure"
    POLICY_VIOLATION = "policy_violation"
    APPROVAL_REJECTED = "approval_rejected"
    REPLAN_LIMIT_REACHED = "replan_limit_reached"
    REPLAN_REQUIRED = "replan_required"
    APPROVAL_PENDING = "approval_pending"
    OPERATOR_STOP = "operator_stop"


def _digest(value: Any) -> str:
    """Stable content digest for change detection.

    ``default=str`` keeps this total over arbitrary agent output; the digest
    only needs to be stable and collision-resistant enough to answer "did this
    value change", not to round-trip.
    """
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


class ContextEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: Any
    digest: str
    revision: int = 1
    written_by: str
    written_at: datetime = Field(default_factory=utcnow)
    read_by: set[str] = Field(default_factory=set)


class ContextStore(BaseModel):
    """Cross-stage context with read/write provenance.

    Every ``get`` that names a reader is recorded. When a stage re-runs and
    changes a key, :meth:`consumers_of` names exactly the stages that consumed
    the previous value — the input to dynamic re-planning.
    """

    model_config = ConfigDict(extra="forbid")

    entries: dict[str, ContextEntry] = Field(default_factory=dict)

    def set(self, key: str, value: Any, *, writer: str) -> bool:
        """Write a key. Returns True if the stored value actually changed.

        An unchanged write is a no-op for lineage: re-running a deterministic
        stage should not invalidate everything downstream of it.
        """
        digest = _digest(value)
        existing = self.entries.get(key)
        if existing is not None and existing.digest == digest:
            return False
        self.entries[key] = ContextEntry(
            key=key,
            value=value,
            digest=digest,
            revision=(existing.revision + 1) if existing else 1,
            written_by=writer,
            # Readers of the *previous* value are preserved so the engine can
            # still identify who consumed the stale revision.
            read_by=set(existing.read_by) if existing else set(),
        )
        return True

    def get(self, key: str, default: Any = None, *, reader: str | None = None) -> Any:
        entry = self.entries.get(key)
        if entry is None:
            return default
        if reader is not None:
            entry.read_by.add(reader)
        return entry.value

    def has(self, key: str) -> bool:
        return key in self.entries

    def consumers_of(self, key: str) -> set[str]:
        entry = self.entries.get(key)
        return set(entry.read_by) if entry else set()

    def revision(self, key: str) -> int:
        entry = self.entries.get(key)
        return entry.revision if entry else 0

    def writer_of(self, key: str) -> str | None:
        entry = self.entries.get(key)
        return entry.written_by if entry else None

    def clear_readers(self, key: str) -> None:
        """Called once stale consumers have been re-queued, so the next change
        does not re-invalidate stages that have already caught up."""
        if entry := self.entries.get(key):
            entry.read_by.clear()

    def keys(self) -> set[str]:
        return set(self.entries)


class StageState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: StageStatus = StageStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    result: StageResult | None = None
    snapshot_id: str | None = None
    gate_failures: list[str] = Field(default_factory=list)
    last_error: str | None = None
    fallback_used: bool = False
    rollbacks: int = 0

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()


class RunState(BaseModel):
    """Everything the engine needs to resume, audit or re-plan a run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    requirement: Requirement
    status: RunStatus = RunStatus.PENDING
    halt_reason: HaltReason | None = None
    halt_detail: str | None = None

    stages: dict[str, StageState] = Field(default_factory=dict)
    context: ContextStore = Field(default_factory=ContextStore)

    normalized: NormalizedRequirement | None = None
    plan: Plan | None = None

    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    approval_log: ApprovalLog = Field(default_factory=ApprovalLog)

    started_at: datetime | None = None
    ended_at: datetime | None = None
    replan_count: int = 0
    ledger_head: str | None = None

    # -- accessors ---------------------------------------------------------

    def stage(self, name: str) -> StageState:
        if name not in self.stages:
            self.stages[name] = StageState(name=name)
        return self.stages[name]

    def statuses(self) -> dict[str, StageStatus]:
        return {name: st.status for name, st in self.stages.items()}

    def names_with_status(self, *statuses: StageStatus) -> set[str]:
        wanted = set(statuses)
        return {name for name, st in self.stages.items() if st.status in wanted}

    @property
    def satisfied(self) -> set[str]:
        return {n for n, st in self.stages.items() if st.status.satisfies_dependents}

    @property
    def failed(self) -> set[str]:
        return self.names_with_status(StageStatus.FAILED, StageStatus.BLOCKED)

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        return ((self.ended_at or utcnow()) - self.started_at).total_seconds()

    # -- recording ---------------------------------------------------------

    def absorb(self, result: StageResult, *, writer: str) -> list[str]:
        """Fold a stage result into run state. Returns changed context keys.

        The engine calls this only after the exit gates have passed, so run
        state never carries output that failed validation.
        """
        for artifact in result.artifacts:
            self.artifacts[artifact.path] = artifact
        self.findings.extend(result.findings)
        self.decisions.extend(result.decisions)

        changed: list[str] = []
        for key, value in result.context_updates.items():
            if self.context.set(key, value, writer=writer):
                changed.append(key)
        return changed

    # -- persistence -------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic: a crash mid-write cannot corrupt the run

    @classmethod
    def load(cls, path: Path) -> RunState:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
