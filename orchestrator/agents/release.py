"""Stage 9: Release Readiness / Final Engineering Summary.

This is the assessment's §4.8 deliverable made concrete: plan and rationale,
artifacts produced, risks/trade-offs/validation, assumptions, and
limitations, assembled from what every prior stage actually recorded (not
re-derived or asserted). It is also the run's `high_impact` node, which is
what makes this the mandatory human checkpoint: no autonomy level can skip
it (see `ApprovalPolicy` -- a declared high-impact stage overrides the
configured autonomy), so nothing reaches "released" without a human having
looked at exactly this document.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orchestrator.agents.base import Agent
from orchestrator.contracts import ArtifactKind, NormalizedRequirement, Severity

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState


class ReleaseAgent(Agent):
    stage_name = "release"

    async def run(self, node: StageNode, state: RunState) -> object:
        raw = state.context.get("normalized_requirement", reader=self.stage_name)
        nreq = NormalizedRequirement.model_validate(raw) if raw else None
        test_report = state.context.get("test_report", reader=self.stage_name) or {}

        blockers = [f for f in state.findings if f.severity is Severity.BLOCKER]
        summary_doc = self._render(state, nreq, test_report, blockers)
        artifact = self.artifact("docs/engineering_summary.md", ArtifactKind.REPORT, summary_doc)

        decision = self.decision(
            "is this run ready to present as a reviewable engineering outcome?",
            "yes" if not blockers else f"no -- {len(blockers)} unresolved blocker(s)",
            f"{len(state.artifacts)} artifact(s) produced across {len(state.decisions)} "
            f"recorded decision(s); test status: {test_report}",
            confidence=0.8,
        )

        return self.result(
            summary=f"engineering summary compiled; {len(blockers)} blocker(s) outstanding",
            artifacts=(artifact,),
            decisions=(decision,),
        )

    def _render(self, state: RunState, nreq, test_report, blockers) -> str:
        req = state.requirement
        lines = [
            "# Final Engineering Summary",
            "",
            f"**Requirement:** {req.title} ({req.kind.value})",
            f"> {req.statement}",
            "",
            "## Plan and rationale",
        ]
        for d in state.decisions:
            lines.append(f"- **[{d.stage}] {d.question}** -> {d.choice}")
            lines.append(f"  - {d.rationale}")
        lines += ["", "## Artifacts produced", ""]
        for path in sorted(state.artifacts):
            art = state.artifacts[path]
            lines.append(f"- `{path}` ({art.kind.value}, by {art.produced_by})")
        lines += ["", "## Assumptions"]
        if nreq and nreq.assumptions:
            for a in nreq.assumptions:
                lines.append(f"- {a}")
        else:
            lines.append("- none recorded")
        lines += ["", "## Risks, trade-offs and validation"]
        lines.append(
            f"- test suite: {test_report.get('passed', 0)} passed, "
            f"{test_report.get('failed', 0)} failed"
        )
        by_severity: dict[str, int] = {}
        for f in state.findings:
            by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1
        lines.append(f"- findings by severity: {by_severity or 'none'}")
        lines.append("- see `docs/risk_register.md` and `docs/security_review.md` for detail")
        lines += ["", "## Limitations"]
        lines += [
            "- SQLite, single-instance rate limiting, and no authentication are documented "
            "prototype-scope trade-offs, not oversights (see docs/risk_register.md)",
            "- generated code covers the functional requirements identified by the "
            "requirements stage; it has not been reviewed by a human engineer beyond the "
            "automated gates and this summary",
        ]
        lines += ["", "## Release recommendation"]
        if blockers:
            lines.append(f"**DO NOT RELEASE** -- {len(blockers)} unresolved blocker(s):")
            for b in blockers:
                lines.append(f"  - [{b.severity.value}] {b.summary}")
        else:
            lines.append("No unresolved blockers. Recommended for human approval and release.")
        return "\n".join(lines) + "\n"
