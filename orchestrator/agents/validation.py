"""Stage 8: Validation and Risk Control.

Assembles the risk register the assessment asks for explicitly (§4.6):
risks, trade-offs, and failure scenarios, each paired with the guardrail
that mitigates it or an explicit statement that none exists yet. This stage
does not re-run tests or scans -- testing and security already did that and
their findings are pulled in here -- it *synthesizes* the accumulated
evidence (ambiguity assumptions, security findings, test results, explicit
design trade-offs) into the single document a human approver actually needs
to make the release call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orchestrator.agents.base import Agent
from orchestrator.contracts import ArtifactKind, NormalizedRequirement, Severity

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState


class ValidationAgent(Agent):
    stage_name = "validation"

    async def run(self, node: StageNode, state: RunState) -> object:
        raw = state.context.get("normalized_requirement", reader=self.stage_name)
        nreq = NormalizedRequirement.model_validate(raw) if raw else None
        test_report = state.context.get("test_report", reader=self.stage_name) or {}
        security_report = state.context.get("security_report", reader=self.stage_name) or {}

        risks = self._compile_risks(state, nreq)
        report = self._render(nreq, test_report, security_report, risks, state)
        artifact = self.artifact("docs/risk_register.md", ArtifactKind.REPORT, report)

        unresolved_blockers = [
            f for f in state.findings if f.severity is Severity.BLOCKER
        ]
        total_tests = test_report.get("passed", 0) + test_report.get("failed", 0)
        recommendation = (
            "no -- unresolved blocker(s) present"
            if unresolved_blockers
            else "yes, with documented risk"
        )
        decision = self.decision(
            "is the accumulated evidence sufficient to recommend release?",
            recommendation,
            f"{len(risks)} risk(s)/trade-off(s) recorded; "
            f"{test_report.get('passed', 0)}/{total_tests} test(s) passing; "
            f"{security_report.get('finding_count', 0)} security finding(s)",
            confidence=0.75,
        )

        return self.result(
            summary=f"risk register compiled: {len(risks)} item(s)",
            artifacts=(artifact,),
            decisions=(decision,),
            context={"risk_count": len(risks)},
        )

    def _compile_risks(self, state: RunState, nreq: NormalizedRequirement | None) -> list[dict]:
        risks: list[dict] = []

        for amb in (nreq.ambiguities if nreq else ()):
            risks.append(
                {
                    "risk": amb.question,
                    "trade_off": amb.assumption,
                    "mitigation": "documented assumption; revisit if usage patterns contradict it"
                    if not amb.blocking
                    else "BLOCKED on human clarification -- do not treat the assumption as safe",
                    "severity": "high" if amb.blocking else "low",
                }
            )

        by_category: dict[str, list] = {}
        for finding in state.findings:
            by_category.setdefault(finding.category, []).append(finding)
        for category, findings in by_category.items():
            worst = max(findings, key=lambda f: f.severity.rank)
            trade_off = f"{len(findings)} finding(s) in this category, worst={worst.severity.value}"
            risks.append(
                {
                    "risk": f"{category}: {worst.summary}",
                    "trade_off": trade_off,
                    "mitigation": worst.remediation or "no remediation recorded",
                    "severity": worst.severity.value,
                }
            )

        risks.append(
            {
                "risk": "SQLite is a single-file, single-writer database",
                "trade_off": "chosen for zero operational overhead in a prototype",
                "mitigation": "the repository interface in app/storage.py is the seam a "
                "Postgres migration would go through without touching route handlers",
                "severity": "medium",
            }
        )
        risks.append(
            {
                "risk": "the rate limiter's state is in-process and per-instance",
                "trade_off": "avoids an external dependency (Redis) at prototype scale",
                "mitigation": "not distributed-safe; replace with a shared store before "
                "running more than one instance",
                "severity": "medium",
            }
        )
        return risks

    def _render(self, nreq, test_report, security_report, risks, state: RunState) -> str:
        lines = ["# Risk Register", ""]
        lines.append(
            f"**Test status:** {test_report.get('passed', 0)} passed, "
            f"{test_report.get('failed', 0)} failed (ran={test_report.get('ran', False)})"
        )
        lines.append(f"**Security findings:** {security_report.get('finding_count', 0)}")
        lines.append(f"**Overall requirement risk:** {nreq.risk.value if nreq else 'unknown'}")
        lines.append("")
        lines.append("## Risks, trade-offs and mitigations")
        for r in sorted(risks, key=lambda r: r["severity"], reverse=True):
            lines.append(f"- **[{r['severity']}] {r['risk']}**")
            lines.append(f"  - trade-off: {r['trade_off']}")
            lines.append(f"  - mitigation: {r['mitigation']}")
        lines.append("")
        lines.append("## Failure scenarios considered")
        lines += [
            "- a request for an unknown code returns 404 rather than a 500 or a silent redirect "
            "to a default page",
            "- a duplicate custom alias is rejected with 409 rather than silently overwriting "
            "the existing mapping",
            "- an expired link returns 410 rather than continuing to redirect indefinitely",
            "- a malformed long_url is rejected with 422 at the boundary rather than stored and "
            "failing later at redirect time",
            "- create-endpoint abuse is bounded by the rate limiter rather than unbounded",
        ]
        return "\n".join(lines) + "\n"
