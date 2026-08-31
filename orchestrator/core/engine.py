"""The orchestration engine.

Execution model
---------------
The engine is a **frontier scheduler**, not a layer walker. On every tick it
dispatches any stage whose join condition is satisfied and awaits the *first*
completion, so a fast branch never blocks behind a slow sibling in the same
topological layer. Synchronization happens where the graph says it should — at
a :attr:`JoinPolicy.ALL` node — rather than implicitly at every layer edge.

Per-stage transactionality
--------------------------
Each stage runs against a workspace snapshot taken immediately before dispatch.
Artifacts are written so that exit gates can inspect real files, but if a gate
rejects the result the snapshot is restored and nothing is folded into run
state. A stage therefore either lands completely or leaves no trace — which is
what makes retry and rollback safe rather than merely hopeful.

Autonomy boundaries
-------------------
An agent returns a :class:`StageResult`; it never mutates :class:`RunState`.
The engine decides admissibility. Where an agent signals that a human must
weigh in, the engine's default is to *stop*, not to proceed — an unattended
system should fail closed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path

from orchestrator.config import OrchestratorConfig
from orchestrator.contracts import StageOutcome, StageResult, new_id, utcnow
from orchestrator.core.gates import GateDecision
from orchestrator.core.graph import StageGraph, StageNode
from orchestrator.core.ledger import EventType, Ledger
from orchestrator.core.state import (
    HaltReason,
    RunState,
    RunStatus,
    StageStatus,
)
from orchestrator.core.workspace import Workspace

StageExecutor = Callable[[StageNode, RunState], Awaitable[StageResult]]

# How long a dispatch tick waits before re-checking halt flags and the deadline.
_TICK_SECONDS = 0.25


class StageRejected(Exception):
    """An exit gate refused the stage's output."""

    def __init__(self, decisions: list[GateDecision]) -> None:
        self.decisions = decisions
        super().__init__("; ".join(f"{d.gate}: {d.reason}" for d in decisions))


class Engine:
    def __init__(
        self,
        graph: StageGraph,
        executor: StageExecutor,
        *,
        config: OrchestratorConfig | None = None,
        workspace: Workspace | None = None,
        ledger: Ledger | None = None,
        run_id: str | None = None,
    ) -> None:
        self.graph = graph
        self.executor = executor
        self.config = config or OrchestratorConfig()
        self.run_id = run_id or new_id("run")

        run_dir = Path(self.config.run_root) / self.run_id
        self.workspace = workspace or Workspace(run_dir / "workspace")
        self.ledger = ledger or Ledger(self.run_id, path=run_dir / "ledger.jsonl")
        self.state_path = run_dir / "state.json"

        self._halt_reason: HaltReason | None = None
        self._halt_detail: str | None = None
        self._deadline: float | None = None

    # -- control -----------------------------------------------------------

    def request_stop(
        self, reason: HaltReason = HaltReason.OPERATOR_STOP, detail: str = ""
    ) -> None:
        """Safe-stop. Idempotent, and the *first* reason wins — a cascade of
        follow-on failures must not overwrite the root cause in the audit
        trail."""
        if self._halt_reason is None:
            self._halt_reason = reason
            self._halt_detail = detail or reason.value

    @property
    def halting(self) -> bool:
        return self._halt_reason is not None

    # -- main loop ---------------------------------------------------------

    async def run(self, state: RunState) -> RunState:
        state.run_id = self.run_id
        state.status = RunStatus.RUNNING
        state.started_at = utcnow()
        for name in self.graph.names:
            state.stage(name)

        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + self.config.run_deadline_seconds

        self.ledger.append(
            EventType.RUN_STARTED,
            summary=state.requirement.title,
            payload={
                "requirement_id": state.requirement.id,
                "kind": state.requirement.kind.value,
                "stages": list(self.graph.names),
                "autonomy": self.config.autonomy.value,
            },
        )

        in_flight: dict[str, asyncio.Task[StageResult | None]] = {}
        try:
            while True:
                self._check_deadline(loop)
                if self.halting:
                    # Stop dispatching immediately; `_drain` applies the grace
                    # window uniformly rather than letting stragglers run on.
                    break

                self._dispatch(state, in_flight)
                if not in_flight:
                    # Nothing running, and nothing new could start. The run is
                    # over -- either complete, or with an unreachable remainder.
                    break

                await self._await_one(in_flight, state)
                self._settle(state)
                state.save(self.state_path)
        finally:
            await self._drain(in_flight, state)
            self._settle(state)
            self._finalize(state)

        return state

    # -- dispatch ----------------------------------------------------------

    def _pending(self, state: RunState) -> set[str]:
        return {n for n in self.graph.names if state.stage(n).status is StageStatus.PENDING}

    def _dispatchable(self, state: RunState) -> set[str]:
        return self.graph.ready(state.satisfied, pending=self._pending(state))

    def _dispatch(self, state: RunState, in_flight: dict[str, asyncio.Task]) -> None:
        for name in sorted(self._dispatchable(state)):
            if len(in_flight) >= self.config.max_parallel_stages:
                return
            node = self.graph[name]
            stage_state = state.stage(name)

            blocked = self._check_entry_gates(node, state)
            if blocked is not None:
                self._mark_entry_blocked(node, state, blocked)
                continue

            # Pre-read declared inputs with reader attribution, so provenance is
            # recorded by the engine rather than trusted to the agent.
            for key in node.consumes:
                state.context.get(key, reader=name)

            stage_state.status = StageStatus.RUNNING
            stage_state.started_at = utcnow()
            stage_state.attempts += 1
            snapshot = self.workspace.snapshot(f"pre:{name}")
            stage_state.snapshot_id = snapshot.id

            self.ledger.append(
                EventType.STAGE_ENTERED,
                stage=name,
                summary=node.title,
                payload={"attempt": stage_state.attempts, "snapshot": snapshot.id},
            )
            in_flight[name] = asyncio.create_task(self._run_stage(node, state), name=name)

    def _check_entry_gates(self, node: StageNode, state: RunState) -> GateDecision | None:
        for gate in node.entry_gates:
            decision = gate.check(node, state)
            if not decision:
                return decision
            self.ledger.append(
                EventType.GATE_ENTRY_PASSED,
                stage=node.name,
                actor=gate.name,
                summary=decision.reason,
            )
        return None

    def _mark_entry_blocked(
        self, node: StageNode, state: RunState, decision: GateDecision
    ) -> None:
        stage_state = state.stage(node.name)
        stage_state.status = StageStatus.BLOCKED
        stage_state.gate_failures.append(f"{decision.gate}: {decision.reason}")
        stage_state.ended_at = utcnow()
        self.ledger.append(
            EventType.GATE_ENTRY_BLOCKED,
            stage=node.name,
            actor=decision.gate,
            summary=decision.reason,
            payload={"remediation": decision.remediation},
        )
        if node.critical:
            self.request_stop(
                HaltReason.BLOCKING_FAILURE,
                f"entry gate {decision.gate} blocked critical stage {node.name}: {decision.reason}",
            )

    # -- stage execution ---------------------------------------------------

    async def _run_stage(self, node: StageNode, state: RunState) -> StageResult | None:
        """One attempt. Phase 3 wraps this with retry, fallback and rollback;
        keeping the single-attempt path isolated is what makes that composable.
        """
        return await self._attempt_stage(node, state)

    async def _attempt_stage(self, node: StageNode, state: RunState) -> StageResult:
        result = await asyncio.wait_for(
            self.executor(node, state), timeout=self.config.stage_timeout_seconds
        )
        if result.stage != node.name:
            result = result.model_copy(update={"stage": node.name})

        # Write artifacts so exit gates can inspect real files. A rejection
        # restores the pre-stage snapshot, so this is not a partial commit.
        sealed = tuple(self.workspace.write_artifact(a) for a in result.artifacts)
        result = result.model_copy(update={"artifacts": sealed})

        # Evaluate each gate exactly once: a gate is allowed to be expensive,
        # and re-running it for the log could report a different reason.
        decisions = [gate.check(node, state, result) for gate in node.exit_gates]
        if rejections := [d for d in decisions if not d]:
            raise StageRejected(rejections)

        for decision in decisions:
            self.ledger.append(
                EventType.GATE_EXIT_PASSED,
                stage=node.name,
                actor=decision.gate,
                summary=decision.reason,
            )
        return result

    # -- completion handling -----------------------------------------------

    async def _await_one(self, in_flight: dict[str, asyncio.Task], state: RunState) -> None:
        remaining = self._time_left()
        timeout = _TICK_SECONDS if remaining is None else max(0.0, min(remaining, _TICK_SECONDS))
        done, _ = await asyncio.wait(
            in_flight.values(), return_when=asyncio.FIRST_COMPLETED, timeout=timeout
        )
        for task in done:
            in_flight.pop(task.get_name(), None)
            self._resolve(task, state)

    def _resolve(self, task: asyncio.Task, state: RunState) -> None:
        name = task.get_name()
        node = self.graph[name]
        stage_state = state.stage(name)
        stage_state.ended_at = utcnow()

        if task.cancelled():
            self._fail_stage(node, state, "cancelled during safe-stop")
            return

        error = task.exception()
        if error is not None:
            self._on_stage_error(node, state, error)
            return

        result = task.result()
        if result is None:
            self._fail_stage(node, state, "stage produced no result")
            return

        if result.outcome is StageOutcome.NEEDS_APPROVAL:
            self._hold_for_approval(node, state, result)
            return
        if result.outcome is StageOutcome.NEEDS_REPLAN:
            self._request_replan(node, state, result)
            return

        self._succeed_stage(node, state, result)

    def _succeed_stage(self, node: StageNode, state: RunState, result: StageResult) -> None:
        stage_state = state.stage(node.name)
        changed = state.absorb(result, writer=node.name)
        stage_state.result = result
        stage_state.status = StageStatus.SUCCEEDED

        for artifact in result.artifacts:
            self.ledger.append(
                EventType.ARTIFACT_WRITTEN,
                stage=node.name,
                summary=artifact.path,
                payload={"kind": artifact.kind.value, "hash": artifact.content_hash},
            )
        for decision in result.decisions:
            self.ledger.append(
                EventType.DECISION_RECORDED,
                stage=node.name,
                summary=decision.question,
                payload={"choice": decision.choice, "rationale": decision.rationale},
                caused_by=decision.derived_from,
            )
        for finding in result.findings:
            self.ledger.append(
                EventType.FINDING_RAISED,
                stage=node.name,
                actor=finding.raised_by or node.name,
                summary=f"[{finding.severity.value}] {finding.summary}",
                payload={"category": finding.category, "path": finding.path},
            )
        self.ledger.append(
            EventType.STAGE_SUCCEEDED,
            stage=node.name,
            summary=result.summary,
            payload={
                "artifacts": len(result.artifacts),
                "context_changed": changed,
                "duration_s": stage_state.duration_seconds,
            },
        )

    def _on_stage_error(self, node: StageNode, state: RunState, error: BaseException) -> None:
        if isinstance(error, StageRejected):
            stage_state = state.stage(node.name)
            stage_state.gate_failures.extend(f"{d.gate}: {d.reason}" for d in error.decisions)
            for decision in error.decisions:
                self.ledger.append(
                    EventType.GATE_EXIT_BLOCKED,
                    stage=node.name,
                    actor=decision.gate,
                    summary=decision.reason,
                    payload={"remediation": decision.remediation},
                )
            self._rollback_stage(node, state, "exit gate rejected the result")
            self._fail_stage(node, state, str(error))
            return

        if isinstance(error, TimeoutError):
            self._rollback_stage(node, state, "stage timed out")
            self._fail_stage(
                node, state, f"timed out after {self.config.stage_timeout_seconds}s"
            )
            return

        self._rollback_stage(node, state, "stage raised")
        self._fail_stage(node, state, f"{type(error).__name__}: {error}")

    def _rollback_stage(self, node: StageNode, state: RunState, why: str) -> None:
        """Restore the pre-stage snapshot so a rejected attempt leaves no trace."""
        stage_state = state.stage(node.name)
        if stage_state.snapshot_id is None:
            return
        self.ledger.append(
            EventType.ROLLBACK_STARTED, stage=node.name, summary=why,
            payload={"snapshot": stage_state.snapshot_id},
        )
        changed = self.workspace.restore(stage_state.snapshot_id)
        stage_state.rollbacks += 1
        self.ledger.append(
            EventType.ROLLBACK_COMPLETED,
            stage=node.name,
            summary=f"reverted {len(changed)} path(s)",
            payload={"paths": changed},
        )

    def _fail_stage(self, node: StageNode, state: RunState, detail: str) -> None:
        """Three distinct failure severities, which the graph declares:

        * ``optional``      -> recorded as SKIPPED; the pipeline was designed to
                               work without this stage, so the run can still pass.
        * ``critical=False`` -> FAILED, but execution continues on live branches;
                               the run finishes in a FAILED state.
        * ``critical=True``  -> FAILED and safe-stop the whole run.
        """
        stage_state = state.stage(node.name)
        stage_state.last_error = detail

        if node.optional:
            stage_state.status = StageStatus.SKIPPED
            self.ledger.append(
                EventType.STAGE_SKIPPED,
                stage=node.name,
                summary=f"optional stage bypassed after failure: {detail}",
            )
            return

        stage_state.status = StageStatus.FAILED
        self.ledger.append(EventType.STAGE_FAILED, stage=node.name, summary=detail)
        if node.critical:
            self.request_stop(
                HaltReason.BLOCKING_FAILURE, f"critical stage {node.name} failed: {detail}"
            )

    def _hold_for_approval(self, node: StageNode, state: RunState, result: StageResult) -> None:
        """Fail closed. An unattended run must not walk past a checkpoint that
        the agent itself flagged as needing a human."""
        stage_state = state.stage(node.name)
        stage_state.status = StageStatus.AWAITING_APPROVAL
        stage_state.result = result
        self.ledger.append(
            EventType.APPROVAL_REQUESTED,
            stage=node.name,
            summary=result.summary or "stage requires human approval",
        )
        self.request_stop(
            HaltReason.APPROVAL_REJECTED,
            f"stage {node.name} is awaiting human approval",
        )

    def _request_replan(self, node: StageNode, state: RunState, result: StageResult) -> None:
        stage_state = state.stage(node.name)
        stage_state.result = result
        stage_state.status = StageStatus.SUCCEEDED
        self.ledger.append(
            EventType.REPLAN_TRIGGERED,
            stage=node.name,
            summary=result.replan_reason or "upstream outputs changed",
        )
        self.request_stop(
            HaltReason.REPLAN_REQUIRED,
            f"stage {node.name} requested a re-plan: {result.replan_reason}",
        )

    # -- settlement --------------------------------------------------------

    def _settle(self, state: RunState) -> None:
        """Mark everything that can never become ready, so the loop terminates
        instead of spinning on a permanently unsatisfiable frontier."""
        resolution = self.graph.resolve_unreachable(state.satisfied, state.failed)

        for name in sorted(resolution.blocked):
            stage_state = state.stage(name)
            if stage_state.status is StageStatus.PENDING:
                stage_state.status = StageStatus.BLOCKED
                stage_state.ended_at = utcnow()
                self.ledger.append(
                    EventType.STAGE_SKIPPED,
                    stage=name,
                    summary="unreachable: a required upstream dependency will never satisfy",
                )

        for name in sorted(resolution.bypassed):
            stage_state = state.stage(name)
            if stage_state.status is StageStatus.PENDING:
                stage_state.status = StageStatus.SKIPPED
                stage_state.ended_at = utcnow()
                self.ledger.append(
                    EventType.STAGE_SKIPPED,
                    stage=name,
                    summary="optional stage bypassed: upstream did not satisfy",
                )

    def _check_deadline(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._deadline is not None and loop.time() >= self._deadline:
            self.request_stop(
                HaltReason.DEADLINE_EXCEEDED,
                f"run exceeded {self.config.run_deadline_seconds}s",
            )

    def _time_left(self) -> float | None:
        if self._deadline is None:
            return None
        return self._deadline - asyncio.get_running_loop().time()

    async def _drain(self, in_flight: dict[str, asyncio.Task], state: RunState) -> None:
        """Let in-flight stages finish within the grace window, then cancel.

        Cancelling immediately is what makes a stop unsafe: a stage killed
        mid-write leaves the workspace in a state no snapshot describes.
        """
        if not in_flight:
            return
        tasks = list(in_flight.values())
        self.ledger.append(
            EventType.SAFE_STOP,
            summary=f"draining {len(tasks)} in-flight stage(s)",
            payload={"grace_s": self.config.safe_stop_grace_seconds},
        )
        done, pending = await asyncio.wait(
            tasks, timeout=self.config.safe_stop_grace_seconds
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in list(done) + list(pending):
            in_flight.pop(task.get_name(), None)
            self._resolve(task, state)

    def _finalize(self, state: RunState) -> None:
        state.ended_at = utcnow()
        state.ledger_head = self.ledger.head

        # A run is only SUCCEEDED if every non-optional stage completed. A
        # stage that failed but was merely non-critical still means the run did
        # not do what it set out to do -- reporting that as success would make
        # the audit trail lie.
        broken = sorted(
            n
            for n in self.graph.names
            if not self.graph[n].optional
            and state.stage(n).status in {StageStatus.FAILED, StageStatus.BLOCKED}
        )

        if self._halt_reason is not None:
            state.status = RunStatus.HALTED
            state.halt_reason = self._halt_reason
            state.halt_detail = self._halt_detail
            if self._halt_reason is HaltReason.APPROVAL_REJECTED and any(
                state.stage(n).status is StageStatus.AWAITING_APPROVAL for n in self.graph.names
            ):
                state.status = RunStatus.AWAITING_APPROVAL
            self.ledger.append(
                EventType.RUN_HALTED,
                summary=self._halt_detail or "",
                payload={"reason": self._halt_reason.value},
            )
        elif broken:
            state.status = RunStatus.FAILED
            self.ledger.append(
                EventType.RUN_FAILED,
                summary=f"stage(s) did not complete: {', '.join(broken)}",
                payload={"broken": broken},
            )
        else:
            state.status = RunStatus.SUCCEEDED
            self.ledger.append(
                EventType.RUN_COMPLETED,
                summary=f"{len(state.satisfied)}/{len(self.graph)} stages satisfied",
                payload={"duration_s": state.duration_seconds},
            )

        state.ledger_head = self.ledger.head
        state.save(self.state_path)
