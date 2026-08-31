"""Stage 6: Security Review.

A dedicated defense-in-depth pass over everything the run has produced so
far, not just what this one stage generates. The universal policy guardrails
(`orchestrator.core.policy`) already run on every stage's own output as part
of the engine's exit pipeline; this stage's job is different -- it looks at
the *accumulated* codebase for cross-cutting issues that only show up once
several files exist together (e.g. an endpoint with no rate limit at all,
not just one line that happens to be dangerous), and produces the artifact a
human reviewer actually reads: a structured security report, not a list of
regex hits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orchestrator.agents.base import Agent
from orchestrator.contracts import ArtifactKind, Severity, StageResult
from orchestrator.core.policy import PolicyEngine

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState


class SecurityAgent(Agent):
    stage_name = "security"

    async def run(self, node: StageNode, state: RunState) -> object:
        code_ctx = state.context.get("code", reader=self.stage_name) or {}
        caps = code_ctx.get("capabilities", {})

        # Re-run the same universal rules against everything accumulated so
        # far (not just this stage's own output, since this stage produces
        # none): catches an issue that only exists in combination, e.g. a
        # PII-in-logs pattern in one file and a logging call in another that
        # the per-stage exit check never saw side by side.
        policy = PolicyEngine.default()
        findings = []
        for artifact in state.artifacts.values():
            single = StageResult(stage=node.name, artifacts=(artifact,))
            findings.extend(policy.evaluate(node, state, single))

        cross_cutting = self._cross_cutting_review(caps)
        findings.extend(cross_cutting)

        report = self._render(findings, caps)
        artifact = self.artifact("docs/security_review.md", ArtifactKind.REPORT, report)

        decision = self.decision(
            "is the accumulated codebase clear of known-pattern security issues?",
            "yes" if not any(f.severity.rank >= Severity.HIGH.rank for f in findings) else "no",
            f"re-ran {len(policy.rules)} guardrail rule(s) against every artifact produced so "
            f"far, plus a cross-cutting review of endpoints that have no dedicated per-line "
            f"pattern to catch (e.g. missing rate limiting)",
            confidence=0.7,
        )

        blockers = [f for f in findings if f.severity is Severity.BLOCKER]
        summary = (
            f"{len(findings)} finding(s) ({len(blockers)} blocker) across "
            f"{len(state.artifacts)} accumulated artifact(s)"
        )
        return self.result(
            summary=summary,
            artifacts=(artifact,),
            findings=tuple(findings),
            decisions=(decision,),
            context={
                "security_report": {"finding_count": len(findings), "blockers": len(blockers)}
            },
        )

    def _cross_cutting_review(self, caps: dict) -> list:
        findings = []
        if not caps.get("rate_limit", False):
            findings.append(
                self.finding(
                    Severity.MEDIUM,
                    "security",
                    "the create endpoint has no rate limiting",
                    detail="an unauthenticated, unlimited create endpoint is an open door for "
                    "resource exhaustion and for using the service as an open redirect farm",
                    remediation="enable the rate-limiting task, or add an API gateway limit "
                    "in front of the service before any non-prototype deployment",
                )
            )
        findings.append(
            self.finding(
                Severity.MEDIUM,
                "security",
                "the service has no authentication on any endpoint",
                detail="acceptable for this prototype's stated scope, but every write and "
                "delete endpoint is currently open to anyone who can reach the service",
                remediation="add API-key or OAuth2 auth in front of POST/DELETE before "
                "any deployment beyond a local prototype",
            )
        )
        findings.append(
            self.finding(
                Severity.LOW,
                "security",
                "long_url is validated as a well-formed URL but not restricted by scheme or host",
                detail="a client can shorten a javascript:, file:, or internal-network URL; "
                "the redirect will happily forward a victim there",
                remediation="restrict accepted schemes to http/https and consider an "
                "allow/deny list for internal address ranges (SSRF-style open-redirect risk)",
            )
        )
        return findings

    def _render(self, findings, caps: dict) -> str:
        lines = ["# Security Review", "", f"**Capabilities reviewed:** {caps}", "", "## Findings"]
        if not findings:
            lines.append("- none")
        for f in sorted(findings, key=lambda f: -f.severity.rank):
            lines.append(f"- **[{f.severity.value}] {f.summary}**")
            if f.detail:
                lines.append(f"  - {f.detail}")
            if f.remediation:
                lines.append(f"  - remediation: {f.remediation}")
        return "\n".join(lines) + "\n"


