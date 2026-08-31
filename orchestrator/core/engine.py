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
Each stage runs against a workspace snapshot taken immediately before every
attempt. Artifacts are written so that exit gates and policy rules can inspect
real files, but if a gate or a BLOCKER policy finding rejects the result, the
snapshot is restored and nothing is folded into run state. A stage therefore
either lands completely or leaves no trace — retry, fallback and rollback all
build on that guarantee.

Governance pipeline
--------------------
A single attempt passes through, in order: the executor, artifact sealing,
exit gates, universal policy guardrails (security/compliance/change-control —
evaluated on *every* stage, not opt-in), and an exit-point human approval
checkpoint derived from autonomy level, declared impact and assessed risk. Any
stage of this pipeline can reject the attempt; :class:`resilience.classify`
decides whether that rejection is worth retrying. Retries exhausted, a
declared :class:`~orchestrator.core.resilience.FallbackStrategy` gets one try
before the stage is recorded as failed and a cascading rollback invalidates
any already-run stage coupled to it.

Autonomy boundaries
-------------------
An agent returns a :class:`StageResult`; it never mutates :class:`RunState`.
The engine decides admissibility. Where a human must weigh in — because the
agent said so, or because governance requires it — the engine's default is to
*stop*, not to proceed, and the run is resumable: :meth:`Engine.resume` picks
a halted approval checkpoint back up once a decision has been recorded,
without re-invoking the executor. :meth:`Engine.replan` re-queues exactly the
stages whose consumed context went stale, computed from read attribution
already kept in :class:`~orchestrator.core.state.ContextStore` — not a
blanket re-run.

Scope note: governance-derived approval checkpoints are enforced at stage
*exit* only. Entry-point checkpoints (e.g. approval to even attempt a
high-impact action) are modelled in :class:`~orchestrator.core.approvals.
ApprovalPolicy` and are independently testable, but this version of the
engine does not yet block dispatch on them — see the project limitations.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path

from orchestrator.config import OrchestratorConfig
from orchestrator.contracts import StageOutcome, StageResult, new_id, utcnow
from orchestrator.core.approvals import (
    ApprovalPoint,
    ApprovalPolicy,
    ApprovalProvider,
    ApprovalRequest,
    ApprovalResponse,
    AutoApproveProvider,
)
from orchestrator.core.gates import GateDecision
from orchestrator.core.graph import StageGraph, StageNode
from orchestrator.core.ledger import EventType, Ledger
from orchestrator.core.metrics import ReliabilityReport
from orchestrator.core.metrics import compute as compute_metrics
from orchestrator.core.policy import PolicyEngine
from orchestrator.core.replanning import compute_scope
from orchestrator.core.resilience import (
    FailureClass,
    RetryController,
    plan_rollback,
)
from orchestrator.core.resilience import (
    classify as classify_failure,
)
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


class PolicyRejected(Exception):
    """A universal guardrail found a BLOCKER-severity violation."""

    def __init__(self, findings) -> None:
        self.findings = findings
        super().__init__("; ".join(f.summary for f in findings))


class ApprovalRejected(Exception):
    """A human (or an automated policy provider) declined the checkpoint."""

    def __init__(self, response: ApprovalResponse) -> None:
        self.response = response
        super().__init__(f"rejected by {response.approver}: {response.note}")


class ApprovalPending(Exception):
    """The checkpoint has no decision yet. Not a failure: the attempt's
    result is preserved so a later :meth:`Engine.resume` can resolve the
    checkpoint without re-invoking the executor."""

    def __init__(self, request: ApprovalRequest, result: StageResult) -> None:
        self.request = request
        self.result = result
        super().__init__(f"awaiting approval: {request.id}")


class StageExhausted(Exception):
    """Retries and any fallback are exhausted; this wraps the last cause so
    the resolver does not attempt a second rollback of the same attempt."""

    def __init__(self, cause: BaseException, failure_class: FailureClass) -> None:
        self.cause = cause
        self.failure_class = failure_class
        super().__init__(str(cause))


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
        policy_engine: PolicyEngine | None = None,
        approval_provider: ApprovalProvider | None = None,
        retry_controller: RetryController | None = None,
    ) -> None:
        self.graph = graph
        self.executor = executor
        self.config = config or OrchestratorConfig()
        self.run_id = run_id or new_id("run")

        run_dir = Path(self.config.run_root) / self.run_id
        self.workspace = workspace or Workspace(run_dir / "workspace")
        self.ledger = ledger or Ledger(self.run_id, path=run_dir / "ledger.jsonl")
        self.state_path = run_dir / "state.json"

        self.policy_engine = policy_engine or PolicyEngine.default()
        self.approval_provider = approval_provider or AutoApproveProvider()
        self.approval_policy = ApprovalPolicy(self.config)
        self.retry_controller = retry_controller or RetryController(self.config.retry)

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

    def metrics(self, state: RunState) -> ReliabilityReport:
        return compute_metrics(state, self.ledger)

    # -- main loop ---------------------------------------------------------

    async def run(self, state: RunState) -> RunState:
        resuming = state.started_at is not None
        state.run_id = self.run_id
        state.status = RunStatus.RUNNING
        if not resuming:
            state.started_at = utcnow()
        for name in self.graph.names:
            state.stage(name)

        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + self.config.run_deadline_seconds

        if not resuming:
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

    async def resume(self, state: RunState) -> RunState:
        """Continue a halted run.

        Any stage sitting in AWAITING_APPROVAL is resolved first, reusing its
        cached, already-gated-and-policy-checked result rather than
        re-invoking the executor — an approval decision must not depend on
        the agent producing byte-identical output twice. If the checkpoint is
        still unanswered, the run halts again in the same state; the caller
        is expected to have already ensured the provider has an answer to
        give (e.g. a human wrote the response file a
        :class:`~orchestrator.core.approvals.FileApprovalProvider` is
        watching for).
        """
        self._halt_reason = None
        self._halt_detail = None

        for name in sorted(state.names_with_status(StageStatus.AWAITING_APPROVAL)):
            node = self.graph[name]
            stage_state = state.stage(name)
            result = stage_state.result
            if result is None:
                continue

            requirement = self.approval_policy.evaluate(node, state, result, ApprovalPoint.EXIT)
            response = await self._checkpoint(node, state, result, requirement)
            if response is None:
                self._hold_for_approval(node, state, result)
                continue
            if not response.granted:
                self._rollback_stage(node, state, "approval rejected")
                self._fail_stage(
                    node,
                    state,
                    f"approval rejected by {response.approver}: {response.note}",
                    halt_reason=HaltReason.APPROVAL_REJECTED,
                )
                continue
            self._succeed_stage(node, state, result)

        return await self.run(state)

    async def replan(
        self, state: RunState, changed_keys: list[str], *, reason: str = ""
    ) -> RunState:
        """Re-queue exactly the stages made stale by a change to upstream
        context, and resume execution.

        The scope is computed from :class:`ContextStore` read attribution —
        see :mod:`orchestrator.core.replanning` — so a sibling branch that
        never touched the changed keys is left untouched.
        """
        if state.replan_count >= self.config.max_replans:
            self.request_stop(
                HaltReason.REPLAN_LIMIT_REACHED,
                f"max_replans={self.config.max_replans} reached; "
                f"requirement may be too unstable for autonomous execution",
            )
            self._settle(state)
            self._finalize(state)
            return state

        scope = compute_scope(self.graph, state, changed_keys, reason=reason)
        state.replan_count += 1

        self.ledger.append(
            EventType.REPLAN_APPLIED,
            summary=scope.reason,
            payload={
                "revision": state.replan_count,
                "changed_keys": list(changed_keys),
                "directly_stale": sorted(scope.directly_stale),
                "transitively_stale": sorted(scope.transitively_stale),
            },
        )

        for name in scope.stale:
            stage_state = state.stage(name)
            stage_state.status = StageStatus.PENDING
            stage_state.attempts = 0
            stage_state.result = None
            stage_state.gate_failures = []
            stage_state.last_error = None
            stage_state.started_at = None
            stage_state.ended_at = None

        for key in changed_keys:
            state.context.clear_readers(key)

        self._halt_reason = None
        self._halt_detail = None
        return await self.run(state)

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
            self.ledger.append(EventType.STAGE_ENTERED, stage=name, summary=node.title)
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

    # -- stage execution: retry / fallback / governance ---------------------

    async def _run_stage(self, node: StageNode, state: RunState) -> StageResult:
        """The full lifecycle of one dispatch: bounded retry around a single
        attempt, then one fallback try if the budget is exhausted."""
        stage_state = state.stage(node.name)
        attempt = 0

        while True:
            attempt += 1
            stage_state.attempts = attempt
            label = f"pre:{node.name}" if attempt == 1 else f"pre:{node.name}:attempt{attempt}"
            snapshot = self.workspace.snapshot(label)
            stage_state.snapshot_id = snapshot.id
            if attempt > 1:
                self.ledger.append(
                    EventType.STAGE_RETRIED,
                    stage=node.name,
                    summary=f"attempt {attempt}",
                    payload={"snapshot": snapshot.id},
                )

            try:
                return await self._attempt_stage(node, state)
            except ApprovalPending:
                raise  # preserve the result and workspace state; do not roll back
            except Exception as exc:  # noqa: BLE001 - classified below, not swallowed
                failure_class = self._record_attempt_failure(node, state, exc)
                self._rollback_stage(node, state, f"attempt {attempt} failed: {exc}")

                decision = self.retry_controller.decide(node, attempt, failure_class)
                if decision.should_retry:
                    if decision.delay_seconds:
                        await asyncio.sleep(decision.delay_seconds)
                    continue

                if node.fallback is not None and not stage_state.fallback_used:
                    fb_result = await self._try_fallback(node, state, exc)
                    if fb_result is not None:
                        return fb_result

                raise StageExhausted(exc, failure_class) from exc

    def _record_attempt_failure(
        self, node: StageNode, state: RunState, exc: Exception
    ) -> FailureClass:
        """Log the cause-specific ledger event and classify it for the retry
        controller. Separated from rollback so the ledger always shows what
        was rejected before it shows what was reverted."""
        stage_state = state.stage(node.name)
        if isinstance(exc, StageRejected):
            stage_state.gate_failures.extend(f"{d.gate}: {d.reason}" for d in exc.decisions)
            for d in exc.decisions:
                self.ledger.append(
                    EventType.GATE_EXIT_BLOCKED,
                    stage=node.name,
                    actor=d.gate,
                    summary=d.reason,
                    payload={"remediation": d.remediation},
                )
            return FailureClass.PERMANENT

        if isinstance(exc, PolicyRejected):
            return FailureClass.POLICY  # POLICY_VIOLATION was already logged when raised

        if isinstance(exc, ApprovalRejected):
            return FailureClass.POLICY  # APPROVAL_REJECTED was already logged when raised

        return classify_failure(exc)

    async def _try_fallback(
        self, node: StageNode, state: RunState, cause: Exception
    ) -> StageResult | None:
        stage_state = state.stage(node.name)
        fallback = node.fallback
        assert fallback is not None
        stage_state.fallback_used = True
        self.ledger.append(
            EventType.FALLBACK_ENGAGED,
            stage=node.name,
            actor=fallback.name,
            summary=f"primary path exhausted ({cause}); trying fallback {fallback.name!r}",
        )
        snapshot = self.workspace.snapshot(f"pre:{node.name}:fallback")
        stage_state.snapshot_id = snapshot.id
        try:
            try:
                result = await asyncio.wait_for(
                    fallback.execute(node, state), timeout=self.config.stage_timeout_seconds
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    f"fallback exceeded {self.config.stage_timeout_seconds}s"
                ) from exc
            return await self._finalize_attempt(node, state, result)
        except ApprovalPending:
            raise
        except Exception as exc:  # noqa: BLE001 - fallback itself failed
            self._record_attempt_failure(node, state, exc)
            self._rollback_stage(node, state, f"fallback also failed: {exc}")
            return None

    async def _attempt_stage(self, node: StageNode, state: RunState) -> StageResult:
        try:
            result = await asyncio.wait_for(
                self.executor(node, state), timeout=self.config.stage_timeout_seconds
            )
        except TimeoutError as exc:
            raise TimeoutError(
                f"stage exceeded {self.config.stage_timeout_seconds}s"
            ) from exc
        return await self._finalize_attempt(node, state, result)

    async def _finalize_attempt(
        self, node: StageNode, state: RunState, result: StageResult
    ) -> StageResult:
        """Everything a produced result must pass before it is admissible:
        artifact sealing, exit gates, universal policy, exit-point approval."""
        if result.stage != node.name:
            result = result.model_copy(update={"stage": node.name})

        # Write artifacts so exit gates and policy rules can inspect real
        # files. A rejection anywhere below restores the pre-attempt snapshot.
        sealed = tuple(self.workspace.write_artifact(a) for a in result.artifacts)
        result = result.model_copy(update={"artifacts": sealed})

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

        policy_findings = self.policy_engine.evaluate(node, state, result)
        rule_count = len(self.policy_engine.rules)
        self.ledger.append(
            EventType.POLICY_EVALUATED,
            stage=node.name,
            summary=f"{len(policy_findings)} finding(s) from {rule_count} rule(s)",
            payload={"codes": self.policy_engine.codes},
        )
        if policy_findings:
            result = result.model_copy(update={"findings": (*result.findings, *policy_findings)})

        if blockers := PolicyEngine.blockers(policy_findings):
            for finding in blockers:
                self.ledger.append(
                    EventType.POLICY_VIOLATION,
                    stage=node.name,
                    actor=finding.raised_by,
                    summary=finding.summary,
                    payload={"remediation": finding.remediation},
                )
            raise PolicyRejected(blockers)

        requirement = self.approval_policy.evaluate(node, state, result, ApprovalPoint.EXIT)
        if requirement:
            response = await self._checkpoint(node, state, result, requirement)
            if response is None:
                raise ApprovalPending(state.approval_log.pending_for(node.name), result)  # type: ignore[arg-type]
            if not response.granted:
                raise ApprovalRejected(response)

        return result

    async def _checkpoint(
        self,
        node: StageNode,
        state: RunState,
        result: StageResult,
        requirement,
    ) -> ApprovalResponse | None:
        """Ensure a request exists, ask the provider, record whatever comes
        back. Idempotent across retries and resumes: a stage that already has
        an unanswered request reuses it instead of spamming duplicates."""
        request = state.approval_log.pending_for(node.name)
        if request is None:
            request = ApprovalRequest(
                run_id=self.run_id,
                stage=node.name,
                point=requirement.point,
                reason=requirement.reason,
                risk=requirement.risk,
                summary=result.summary,
                artifact_paths=tuple(a.path for a in result.artifacts),
                artifact_digests=tuple(a.content_hash for a in result.artifacts),
                findings=tuple(f"[{f.severity.value}] {f.summary}" for f in result.findings),
                decisions=tuple(f"{d.question} -> {d.choice}" for d in result.decisions),
            )
            state.approval_log.record_request(request)
            self.ledger.append(
                EventType.APPROVAL_REQUESTED,
                stage=node.name,
                summary=requirement.reason,
                payload={"request_id": request.id, "risk": requirement.risk.value},
            )

        response = await self.approval_provider.decide(request)
        if response is None:
            return None

        state.approval_log.record_response(response)
        event_type = (
            EventType.APPROVAL_GRANTED if response.granted else EventType.APPROVAL_REJECTED
        )
        self.ledger.append(
            event_type,
            stage=node.name,
            actor=response.approver,
            summary=response.note,
            payload={"request_id": request.id, "automated": response.automated},
        )
        return response

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
            if isinstance(error, ApprovalPending):
                self._hold_for_approval(node, state, error.result)
                return
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
                "attempts": stage_state.attempts,
                "fallback_used": stage_state.fallback_used,
            },
        )

    def _on_stage_error(self, node: StageNode, state: RunState, error: BaseException) -> None:
        """By the time an error reaches here, `_run_stage` has already
        classified it, logged the cause-specific event, retried within budget
        and tried any fallback. This only needs to record the terminal state
        and pick the halt reason the audit trail should show as root cause.
        """
        cause = error.cause if isinstance(error, StageExhausted) else error
        attempts = state.stage(node.name).attempts
        detail = f"{type(cause).__name__}: {cause}"
        if isinstance(error, StageExhausted):
            detail = f"exhausted after {attempts} attempt(s): {detail}"

        halt_reason = HaltReason.BLOCKING_FAILURE
        if isinstance(cause, ApprovalRejected):
            halt_reason = HaltReason.APPROVAL_REJECTED
        elif isinstance(cause, PolicyRejected):
            halt_reason = HaltReason.POLICY_VIOLATION

        self._fail_stage(node, state, detail, halt_reason=halt_reason)

    def _rollback_stage(self, node: StageNode, state: RunState, why: str) -> None:
        """Restore the pre-attempt snapshot so a rejected attempt leaves no trace."""
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

    def _cascade_rollback(self, node: StageNode, state: RunState) -> None:
        """A stage that fails for good may invalidate work that already ran
        downstream of it, or that is explicitly coupled to it via
        ``rollback_with``. Revert each such stage's own workspace snapshot and
        mark it ROLLED_BACK so it is neither mistaken for success nor silently
        re-dispatched; a deliberate :meth:`Engine.replan` brings it back.
        """
        ran = state.names_with_status(StageStatus.SUCCEEDED)
        plan = plan_rollback(self.graph, node.name, ran)
        if not plan:
            return

        self.ledger.append(
            EventType.ROLLBACK_STARTED,
            stage=node.name,
            summary=f"cascading to {len(plan.stages)} coupled/downstream stage(s)",
            payload={"stages": list(plan.stages)},
        )
        for coupled_name in plan.stages:
            coupled_state = state.stage(coupled_name)
            if coupled_state.snapshot_id is None:
                continue
            changed = self.workspace.restore(coupled_state.snapshot_id)
            coupled_state.rollbacks += 1
            coupled_state.status = StageStatus.ROLLED_BACK
            coupled_state.ended_at = utcnow()
            self.ledger.append(
                EventType.ROLLBACK_COMPLETED,
                stage=coupled_name,
                summary=f"reverted {len(changed)} path(s); invalidated by {node.name}",
                payload={"paths": changed, "trigger": node.name},
            )

    def _fail_stage(
        self,
        node: StageNode,
        state: RunState,
        detail: str,
        *,
        halt_reason: HaltReason = HaltReason.BLOCKING_FAILURE,
    ) -> None:
        """Three distinct failure severities, which the graph declares:

        * ``optional``      -> recorded as SKIPPED; the pipeline was designed to
                               work without this stage, so the run can still pass.
        * ``critical=False`` -> FAILED, but execution continues on live branches;
                               the run finishes in a FAILED state.
        * ``critical=True``  -> FAILED and safe-stop the whole run.

        A non-optional failure also cascades a rollback to whatever already-run
        work was built on this stage — see :meth:`_cascade_rollback`.
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
        self._cascade_rollback(node, state)

        # A governance-driven halt reason (a human rejected a checkpoint, or a
        # BLOCKER policy finding) is mandatory regardless of the node's own
        # criticality: those are decisions about the run as a whole, not
        # ordinary execution failures a non-critical branch can absorb.
        governance_halt = halt_reason in {
            HaltReason.APPROVAL_REJECTED,
            HaltReason.POLICY_VIOLATION,
        }
        if node.critical or governance_halt:
            self.request_stop(halt_reason, f"stage {node.name} failed: {detail}")

    def _hold_for_approval(self, node: StageNode, state: RunState, result: StageResult) -> None:
        """Fail closed. An unattended run must not walk past a checkpoint that
        needs a human, whether the agent flagged it or governance derived it.

        A governance-derived checkpoint (:meth:`_checkpoint`) has already
        logged its own, more detailed APPROVAL_REQUESTED event by the time
        this runs, and already recorded the request in `state.approval_log` --
        logging again here would duplicate the audit trail and double-count
        the reliability metric. Only the legacy path, where the agent itself
        returned ``StageOutcome.NEEDS_APPROVAL`` without ever going through a
        checkpoint, needs this method to create the record.
        """
        stage_state = state.stage(node.name)
        stage_state.status = StageStatus.AWAITING_APPROVAL
        stage_state.result = result
        has_governance_request = any(
            r.stage == node.name for r in state.approval_log.requests.values()
        )
        if not has_governance_request:
            self.ledger.append(
                EventType.APPROVAL_REQUESTED,
                stage=node.name,
                summary=result.summary or "stage requires human approval",
            )
        self.request_stop(
            HaltReason.APPROVAL_PENDING,
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
        # Always reflect the *current* halt reason, including clearing a stale
        # one from a prior halt: resume()/replan() reset the engine's own
        # _halt_reason before re-entering the loop, and if this pass completes
        # without halting again, RunState must not keep reporting the old one.
        state.halt_reason = self._halt_reason
        state.halt_detail = self._halt_detail

        # A run is only SUCCEEDED if every non-optional stage completed and
        # nothing was left rolled back by a cascade. A stage that failed but
        # was merely non-critical still means the run did not do what it set
        # out to do -- reporting that as success would make the audit trail lie.
        broken = sorted(
            n
            for n in self.graph.names
            if not self.graph[n].optional
            and state.stage(n).status
            in {StageStatus.FAILED, StageStatus.BLOCKED, StageStatus.ROLLED_BACK}
        )

        if self._halt_reason is not None:
            state.status = RunStatus.HALTED
            if self._halt_reason is HaltReason.APPROVAL_PENDING and any(
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
