"""Reliability metrics, derived from the ledger and run state.

Every number here is computed after the fact from the audit trail rather than
accumulated as counters during execution. That is a deliberate choice: it
means the metrics can never drift from what actually happened, and the same
computation works whether you ask mid-run, at the end, or by replaying a
persisted ledger days later.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.core.ledger import EventType, Ledger
from orchestrator.core.state import RunState, StageStatus


@dataclass(frozen=True)
class StageMetrics:
    name: str
    attempts: int
    succeeded: bool
    retries: int
    rollbacks: int
    fallback_used: bool
    duration_seconds: float | None
    # Includes retry delay; None if the stage never succeeded.
    time_to_success_seconds: float | None


@dataclass(frozen=True)
class ReliabilityReport:
    run_id: str
    total_stages: int
    succeeded: int
    failed: int
    skipped: int
    blocked: int

    success_rate: float                 # succeeded / (total - optional-skipped)
    retry_count: int
    retry_frequency: float               # retries / total stage attempts
    rollback_count: int
    rollback_frequency: float            # rollbacks / total stage attempts
    fallback_count: int

    # Mean, across retried stages, of first-entry-to-eventual-success duration.
    mttr_seconds: float | None
    end_to_end_latency_seconds: float | None

    approvals_requested: int
    approvals_granted: int
    approvals_rejected: int
    human_approvals: int

    policy_violations: int
    replans_triggered: int

    stages: tuple[StageMetrics, ...] = field(default_factory=tuple)

    def summary_line(self) -> str:
        e2e = self.end_to_end_latency_seconds
        latency = f"{e2e:.2f}s" if e2e is not None else "n/a"
        mttr = f"{self.mttr_seconds:.2f}s" if self.mttr_seconds is not None else "n/a"
        return (
            f"success_rate={self.success_rate:.0%} "
            f"retries={self.retry_count} rollbacks={self.rollback_count} "
            f"mttr={mttr} e2e={latency}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "total_stages": self.total_stages,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "blocked": self.blocked,
            "success_rate": self.success_rate,
            "retry_count": self.retry_count,
            "retry_frequency": self.retry_frequency,
            "rollback_count": self.rollback_count,
            "rollback_frequency": self.rollback_frequency,
            "fallback_count": self.fallback_count,
            "mttr_seconds": self.mttr_seconds,
            "end_to_end_latency_seconds": self.end_to_end_latency_seconds,
            "approvals_requested": self.approvals_requested,
            "approvals_granted": self.approvals_granted,
            "approvals_rejected": self.approvals_rejected,
            "human_approvals": self.human_approvals,
            "policy_violations": self.policy_violations,
            "replans_triggered": self.replans_triggered,
            "stages": [
                {
                    "name": s.name,
                    "attempts": s.attempts,
                    "succeeded": s.succeeded,
                    "retries": s.retries,
                    "rollbacks": s.rollbacks,
                    "fallback_used": s.fallback_used,
                    "duration_seconds": s.duration_seconds,
                    "time_to_success_seconds": s.time_to_success_seconds,
                }
                for s in self.stages
            ],
        }


def _stage_first_entry_at(ledger: Ledger, stage: str):
    entries = ledger.for_stage(stage)
    first = next((e for e in entries if e.type is EventType.STAGE_ENTERED), None)
    return first.at if first else None


def compute(state: RunState, ledger: Ledger) -> ReliabilityReport:
    stage_names = list(state.stages)
    total = len(stage_names)

    succeeded = state.names_with_status(StageStatus.SUCCEEDED)
    failed = state.names_with_status(StageStatus.FAILED)
    skipped = state.names_with_status(StageStatus.SKIPPED)
    blocked = state.names_with_status(StageStatus.BLOCKED)

    # Success rate excludes stages the graph deliberately bypassed: a skip is
    # not a reliability failure, it is the graph working as designed.
    denom = total - len(skipped)
    success_rate = (len(succeeded) / denom) if denom else 1.0

    stage_metrics: list[StageMetrics] = []
    retry_total = 0
    rollback_total = 0
    fallback_total = 0
    ttr_samples: list[float] = []

    for name in stage_names:
        st = state.stage(name)
        retries = max(st.attempts - 1, 0)
        retry_total += retries
        rollback_total += st.rollbacks
        fallback_total += 1 if st.fallback_used else 0

        entered_at = _stage_first_entry_at(ledger, name)
        ttr = None
        succeeded_with_time = (
            st.status is StageStatus.SUCCEEDED
            and entered_at is not None
            and st.ended_at is not None
        )
        if succeeded_with_time:
            ttr = (st.ended_at - entered_at).total_seconds()
            if retries > 0:
                ttr_samples.append(ttr)

        stage_metrics.append(
            StageMetrics(
                name=name,
                attempts=st.attempts,
                succeeded=st.status is StageStatus.SUCCEEDED,
                retries=retries,
                rollbacks=st.rollbacks,
                fallback_used=st.fallback_used,
                duration_seconds=st.duration_seconds,
                time_to_success_seconds=ttr,
            )
        )

    total_attempts = sum(s.attempts for s in stage_metrics) or 1
    approval_requested = len(ledger.of_type(EventType.APPROVAL_REQUESTED))
    approval_granted = len(ledger.of_type(EventType.APPROVAL_GRANTED))
    approval_rejected = len(ledger.of_type(EventType.APPROVAL_REJECTED))
    human_approvals = sum(
        1
        for e in ledger.of_type(EventType.APPROVAL_GRANTED, EventType.APPROVAL_REJECTED)
        if not e.payload.get("automated", False)
    )

    return ReliabilityReport(
        run_id=state.run_id,
        total_stages=total,
        succeeded=len(succeeded),
        failed=len(failed),
        skipped=len(skipped),
        blocked=len(blocked),
        success_rate=success_rate,
        retry_count=retry_total,
        retry_frequency=retry_total / total_attempts,
        rollback_count=rollback_total,
        rollback_frequency=rollback_total / total_attempts,
        fallback_count=fallback_total,
        mttr_seconds=(sum(ttr_samples) / len(ttr_samples)) if ttr_samples else None,
        end_to_end_latency_seconds=state.duration_seconds,
        approvals_requested=approval_requested,
        approvals_granted=approval_granted,
        approvals_rejected=approval_rejected,
        human_approvals=human_approvals,
        policy_violations=len(ledger.of_type(EventType.POLICY_VIOLATION)),
        replans_triggered=len(ledger.of_type(EventType.REPLAN_TRIGGERED)),
        stages=tuple(stage_metrics),
    )
