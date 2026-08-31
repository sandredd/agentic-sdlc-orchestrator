"""Human approval checkpoints.

Controlled autonomy means the *engine* decides when a human must weigh in, and
the agent cannot talk it out of that. :class:`ApprovalPolicy` derives the
requirement from three inputs the agent does not control: the configured
autonomy level, the node's declared impact, and the assessed risk.

Approvals are asynchronous by design. A provider may answer immediately
(auto-approve in CI, deny-all in a locked-down environment) or return ``None``
meaning "pending", in which case the run persists itself and stops. A later
:meth:`Engine.resume` picks it up once a decision is recorded. That is what
makes a checkpoint a real gate rather than a blocking prompt that dies with
the process.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.config import AutonomyLevel, OrchestratorConfig
from orchestrator.contracts import RiskLevel, Severity, new_id, utcnow

if TYPE_CHECKING:
    from orchestrator.contracts import StageResult
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState


class ApprovalPoint(StrEnum):
    """When the checkpoint fires.

    ENTRY is for actions whose *execution* is the high-impact event (a deploy);
    by the time you are reviewing the output it is too late. EXIT is for
    actions whose output is what needs judging (a design, a schema change).
    """

    ENTRY = "entry"
    EXIT = "exit"


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: new_id("apr"))
    run_id: str
    stage: str
    point: ApprovalPoint
    reason: str
    risk: RiskLevel
    requested_at: datetime = Field(default_factory=utcnow)
    summary: str = ""
    artifact_paths: tuple[str, ...] = ()
    artifact_digests: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()

    def brief(self) -> str:
        """Human-readable checkpoint. What a reviewer needs to say yes or no."""
        lines = [
            f"Approval required: {self.stage} ({self.point.value})",
            f"  run:    {self.run_id}",
            f"  risk:   {self.risk.value}",
            f"  reason: {self.reason}",
        ]
        if self.summary:
            lines.append(f"  what:   {self.summary}")
        if self.artifact_paths:
            lines.append(f"  files:  {', '.join(self.artifact_paths)}")
        for decision in self.decisions:
            lines.append(f"  decision: {decision}")
        for finding in self.findings:
            lines.append(f"  finding:  {finding}")
        return "\n".join(lines)


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    granted: bool
    approver: str
    note: str = ""
    at: datetime = Field(default_factory=utcnow)
    # True when no human was actually involved. Recorded so a reliability
    # report can distinguish genuine oversight from a rubber stamp.
    automated: bool = False


class ApprovalProvider(ABC):
    """Returns a decision, or ``None`` to mean "pending; stop and come back"."""

    name: str = "provider"

    @abstractmethod
    async def decide(self, request: ApprovalRequest) -> ApprovalResponse | None: ...


class AutoApproveProvider(ApprovalProvider):
    """For CI and demos. Every response is flagged ``automated`` so it can
    never be mistaken for human oversight in a report."""

    name = "auto"

    def __init__(self, approver: str = "ci-auto") -> None:
        self.approver = approver

    async def decide(self, request: ApprovalRequest) -> ApprovalResponse:
        return ApprovalResponse(
            request_id=request.id,
            granted=True,
            approver=self.approver,
            note="auto-approved: no human in the loop",
            automated=True,
        )


class DenyAllProvider(ApprovalProvider):
    """Locked-down mode: prove the pipeline stops where it should."""

    name = "deny"

    async def decide(self, request: ApprovalRequest) -> ApprovalResponse:
        return ApprovalResponse(
            request_id=request.id,
            granted=False,
            approver="policy",
            note="automated approval is disabled in this environment",
            automated=True,
        )


class CallbackApprovalProvider(ApprovalProvider):
    """Wraps a caller-supplied function. Used by the CLI for an interactive
    prompt and by tests for deterministic decisions."""

    name = "callback"

    def __init__(self, fn: Callable[[ApprovalRequest], ApprovalResponse | None]) -> None:
        self.fn = fn

    async def decide(self, request: ApprovalRequest) -> ApprovalResponse | None:
        return self.fn(request)


class FileApprovalProvider(ApprovalProvider):
    """Out-of-band human approval over the filesystem.

    The request is written as JSON plus a readable brief; the run then stops.
    A human writes ``<request_id>.response.json`` and re-runs ``asdlc resume``.
    Crude on purpose: it survives process restarts, needs no service, and the
    artefacts it leaves behind are exactly what an auditor wants to see.
    """

    name = "file"

    def __init__(self, inbox: Path) -> None:
        self.inbox = Path(inbox)
        self.inbox.mkdir(parents=True, exist_ok=True)

    def _request_path(self, request_id: str) -> Path:
        return self.inbox / f"{request_id}.request.json"

    def _response_path(self, request_id: str) -> Path:
        return self.inbox / f"{request_id}.response.json"

    async def decide(self, request: ApprovalRequest) -> ApprovalResponse | None:
        response_path = self._response_path(request.id)
        if response_path.exists():
            return ApprovalResponse.model_validate_json(
                response_path.read_text(encoding="utf-8")
            )
        self._request_path(request.id).write_text(
            request.model_dump_json(indent=2), encoding="utf-8"
        )
        (self.inbox / f"{request.id}.brief.txt").write_text(
            request.brief() + "\n", encoding="utf-8"
        )
        # Leave a template so the reviewer does not have to guess the schema.
        template = self.inbox / f"{request.id}.response.template.json"
        if not template.exists():
            template.write_text(
                json.dumps(
                    {
                        "request_id": request.id,
                        "granted": True,
                        "approver": "your.name",
                        "note": "why you are approving or rejecting",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        return None


class ApprovalRequirement(BaseModel):
    """Why a checkpoint fired. Recorded verbatim in the ledger so the audit
    trail explains the escalation, not just that one happened."""

    model_config = ConfigDict(extra="forbid")

    required: bool
    point: ApprovalPoint = ApprovalPoint.EXIT
    reason: str = ""
    risk: RiskLevel = RiskLevel.LOW

    def __bool__(self) -> bool:
        return self.required


class ApprovalPolicy:
    """Derives the approval requirement from autonomy, impact and risk.

    The precedence is deliberate: a declared high-impact action outranks the
    configured autonomy level. Raising autonomy to AUTONOMOUS must not be able
    to switch off the checkpoint on a release.
    """

    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config

    def assess_risk(
        self, node: StageNode, state: RunState, result: StageResult | None
    ) -> RiskLevel:
        risk = state.normalized.risk if state.normalized else RiskLevel.MEDIUM
        if result is not None:
            worst = result.max_severity()
            if worst is Severity.BLOCKER:
                risk = RiskLevel.CRITICAL
            elif worst is Severity.HIGH and risk.rank < RiskLevel.HIGH.rank:
                risk = RiskLevel.HIGH
        if node.high_impact and risk.rank < RiskLevel.HIGH.rank:
            risk = RiskLevel.HIGH
        return risk

    def evaluate(
        self, node: StageNode, state: RunState, result: StageResult | None, point: ApprovalPoint
    ) -> ApprovalRequirement:
        risk = self.assess_risk(node, state, result)
        autonomy = self.config.autonomy

        if node.high_impact and node.approval_point is point:
            return ApprovalRequirement(
                required=True,
                point=point,
                risk=risk,
                reason=f"{node.name} is declared high-impact; a human owns this action",
            )

        if autonomy is AutonomyLevel.SUGGEST and point is ApprovalPoint.ENTRY:
            return ApprovalRequirement(
                required=True, point=point, risk=risk,
                reason="autonomy=suggest: agents propose, humans execute",
            )

        if autonomy is AutonomyLevel.SUPERVISED and point is ApprovalPoint.EXIT:
            return ApprovalRequirement(
                required=True, point=point, risk=risk,
                reason="autonomy=supervised: every stage exit is reviewed",
            )

        if (
            autonomy is AutonomyLevel.BOUNDED
            and point is ApprovalPoint.EXIT
            and risk.rank >= self.config.approval_risk_threshold.rank
        ):
            return ApprovalRequirement(
                required=True, point=point, risk=risk,
                reason=(
                    f"risk {risk.value} reached the escalation threshold "
                    f"{self.config.approval_risk_threshold.value}"
                ),
            )

        return ApprovalRequirement(required=False, point=point, risk=risk)


class ApprovalLog(BaseModel):
    """Persisted record of every checkpoint in a run, so a resumed run knows
    what has already been answered."""

    model_config = ConfigDict(extra="forbid")

    requests: dict[str, ApprovalRequest] = Field(default_factory=dict)
    responses: dict[str, ApprovalResponse] = Field(default_factory=dict)

    def record_request(self, request: ApprovalRequest) -> None:
        self.requests[request.id] = request

    def record_response(self, response: ApprovalResponse) -> None:
        self.responses[response.request_id] = response

    def pending(self) -> list[ApprovalRequest]:
        return [r for rid, r in self.requests.items() if rid not in self.responses]

    def pending_for(self, stage: str) -> ApprovalRequest | None:
        return next((r for r in self.pending() if r.stage == stage), None)

    def response_for(self, request_id: str) -> ApprovalResponse | None:
        return self.responses.get(request_id)

    @property
    def granted_count(self) -> int:
        return sum(1 for r in self.responses.values() if r.granted)

    @property
    def rejected_count(self) -> int:
        return sum(1 for r in self.responses.values() if not r.granted)

    @property
    def human_count(self) -> int:
        return sum(1 for r in self.responses.values() if not r.automated)
