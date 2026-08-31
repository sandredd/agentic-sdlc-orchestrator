"""Dynamic re-planning.

When an upstream stage's output changes on a re-run — because a human
answered a previously-blocking ambiguity, or an agent revised a decision — the
question is exactly which downstream work is now built on stale input.

The answer does not require re-deriving anything: :class:`ContextStore`
already recorded who read each key (`orchestrator/core/state.py`). Re-planning
is therefore a small, precise computation over data the engine was already
keeping, not a fresh analysis pass — which is what keeps it fast enough to run
on every upstream change instead of being a manual "re-run everything" button.

The scope this computes is *necessary and sufficient*: every stage that
consumed a changed key (directly or transitively, since a stage that reruns
produces new output of its own that its own consumers must see), and nothing
that did not. Re-running siblings that never touched the changed data would
throw away good work and inflate the very retry/rollback metrics this system
is supposed to keep low.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.core.graph import StageGraph
    from orchestrator.core.state import RunState


@dataclass(frozen=True)
class ReplanScope:
    """The precise set of stages a re-plan must requeue."""

    changed_keys: tuple[str, ...]
    directly_stale: frozenset[str]   # stages that read a changed key
    transitively_stale: frozenset[str]  # + everything downstream of those
    reason: str

    @property
    def stale(self) -> frozenset[str]:
        return self.directly_stale | self.transitively_stale

    def __bool__(self) -> bool:
        return bool(self.stale)


def compute_scope(
    graph: StageGraph, state: RunState, changed_keys: list[str], *, reason: str = ""
) -> ReplanScope:
    """Compute the minimal re-run set for a set of changed context keys."""
    direct: set[str] = set()
    for key in changed_keys:
        direct |= state.context.consumers_of(key)
    # A stage cannot be stale relative to its own write.
    direct -= {state.context.writer_of(k) for k in changed_keys if state.context.writer_of(k)}
    direct &= set(graph.names)

    transitive: set[str] = set()
    for name in direct:
        transitive |= graph.descendants(name)
    transitive -= direct

    return ReplanScope(
        changed_keys=tuple(changed_keys),
        directly_stale=frozenset(direct),
        transitively_stale=frozenset(transitive),
        reason=reason or f"upstream change to {', '.join(changed_keys)}",
    )


@dataclass
class ReplanRecord:
    """One re-plan event, kept on the run for the audit trail and for
    detecting a thrashing loop (the same stage repeatedly going stale)."""

    revision: int
    scope: ReplanScope
    triggered_by: str


@dataclass
class ReplanHistory:
    records: list[ReplanRecord] = field(default_factory=list)

    def record(self, scope: ReplanScope, *, triggered_by: str) -> ReplanRecord:
        rec = ReplanRecord(revision=len(self.records) + 1, scope=scope, triggered_by=triggered_by)
        self.records.append(rec)
        return rec

    def thrash_count(self, stage: str) -> int:
        """How many times a given stage has been re-queued. A high count is
        the signal that the requirement itself is unstable, not the agent."""
        return sum(1 for r in self.records if stage in r.scope.stale)

    @property
    def count(self) -> int:
        return len(self.records)
