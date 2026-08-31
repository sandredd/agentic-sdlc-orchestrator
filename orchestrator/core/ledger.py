"""Audit-grade event ledger.

Two properties make this "audit-grade" rather than "logging":

1. **Append-only, hash-chained.** Each event carries the hash of its
   predecessor. Editing or dropping a historical event breaks the chain, and
   :meth:`Ledger.verify` will point at the first broken link. A reviewer can
   therefore trust that the recorded sequence is the sequence that ran.
2. **Structured causality.** Every event names the stage, the actor and the
   ids it was `caused_by`, so lineage can be reconstructed as a graph instead
   of being reverse-engineered from prose.

The ledger is the single write path for run history. The engine, the gates,
the policy layer and the approval layer all emit through it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.contracts import new_id, utcnow

GENESIS = "0" * 64


class EventType(StrEnum):
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_HALTED = "run.halted"

    STAGE_ENTERED = "stage.entered"
    STAGE_SUCCEEDED = "stage.succeeded"
    STAGE_FAILED = "stage.failed"
    STAGE_SKIPPED = "stage.skipped"
    STAGE_RETRIED = "stage.retried"

    GATE_ENTRY_PASSED = "gate.entry.passed"
    GATE_ENTRY_BLOCKED = "gate.entry.blocked"
    GATE_EXIT_PASSED = "gate.exit.passed"
    GATE_EXIT_BLOCKED = "gate.exit.blocked"

    POLICY_EVALUATED = "policy.evaluated"
    POLICY_VIOLATION = "policy.violation"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_TIMED_OUT = "approval.timed_out"

    FALLBACK_ENGAGED = "fallback.engaged"
    ROLLBACK_STARTED = "rollback.started"
    ROLLBACK_COMPLETED = "rollback.completed"
    SAFE_STOP = "safe_stop"

    REPLAN_TRIGGERED = "replan.triggered"
    REPLAN_APPLIED = "replan.applied"

    ARTIFACT_WRITTEN = "artifact.written"
    DECISION_RECORDED = "decision.recorded"
    FINDING_RAISED = "finding.raised"


class Event(BaseModel):
    """One immutable entry in the chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=lambda: new_id("ev"))
    seq: int
    at: datetime = Field(default_factory=utcnow)
    type: EventType
    run_id: str
    stage: str | None = None
    actor: str = "engine"
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    caused_by: tuple[str, ...] = ()
    prev_hash: str = GENESIS
    hash: str = ""

    def compute_hash(self) -> str:
        """Hash over the canonical, hash-excluded body plus the previous hash.

        `sort_keys` + `default=str` keeps the digest stable across processes
        regardless of field insertion order or datetime repr.
        """
        body = self.model_dump(mode="json", exclude={"hash"})
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def sealed(self) -> Event:
        return self.model_copy(update={"hash": self.compute_hash()})


class ChainBreak(BaseModel):
    seq: int
    event_id: str
    reason: str


class Ledger:
    """Thread-safe append-only chain, optionally mirrored to a JSONL file.

    Writes are guarded by a lock because the engine runs stages concurrently;
    sequence numbers and the hash chain must be linearized even though the work
    that produced the events was parallel.
    """

    def __init__(self, run_id: str, path: Path | None = None) -> None:
        self.run_id = run_id
        self.path = path
        self._events: list[Event] = []
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        type: EventType,
        *,
        stage: str | None = None,
        actor: str = "engine",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        caused_by: tuple[str, ...] = (),
    ) -> Event:
        with self._lock:
            prev = self._events[-1].hash if self._events else GENESIS
            event = Event(
                seq=len(self._events),
                type=type,
                run_id=self.run_id,
                stage=stage,
                actor=actor,
                summary=summary,
                payload=payload or {},
                caused_by=caused_by,
                prev_hash=prev,
            ).sealed()
            self._events.append(event)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(event.model_dump_json() + "\n")
            return event

    # -- reading -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(tuple(self._events))

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def head(self) -> str:
        """Current chain head. Two ledgers with the same head recorded the same
        history — useful as a compact run fingerprint in reports."""
        return self._events[-1].hash if self._events else GENESIS

    def of_type(self, *types: EventType) -> tuple[Event, ...]:
        wanted = set(types)
        return tuple(e for e in self._events if e.type in wanted)

    def for_stage(self, stage: str) -> tuple[Event, ...]:
        return tuple(e for e in self._events if e.stage == stage)

    def verify(self) -> list[ChainBreak]:
        """Re-derive every hash and every link. Empty list means intact.

        The chain is walked on *recomputed* hashes, not the stored ones. That
        matters: if an event body is edited, its recomputed hash changes, so
        every successor's ``prev_hash`` stops matching too. Tampering shows up
        as a break at the edit and at everything downstream of it, which is
        what stops an attacker from forging one event in isolation.
        """
        breaks: list[ChainBreak] = []
        expected_prev = GENESIS
        for idx, event in enumerate(self._events):
            actual = event.compute_hash()
            if event.seq != idx:
                breaks.append(
                    ChainBreak(seq=idx, event_id=event.id, reason="sequence out of order")
                )
            if event.prev_hash != expected_prev:
                breaks.append(
                    ChainBreak(seq=idx, event_id=event.id, reason="prev_hash does not match chain")
                )
            if event.hash != actual:
                breaks.append(
                    ChainBreak(seq=idx, event_id=event.id, reason="event body was modified")
                )
            expected_prev = actual
        return breaks

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, run_id: str, path: Path) -> Ledger:
        ledger = cls(run_id, path=None)
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    ledger._events.append(Event.model_validate_json(line))
        ledger.path = path
        return ledger
