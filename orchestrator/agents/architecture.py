"""Stage 2: Architecture & Design.

Produces the API contract and the key structural decisions (storage,
short-code generation, layering) that implementation must follow. For a
brownfield run it does one more thing the assessment calls out separately
(§4.3, Codebase Reasoning): before proposing any change, it scans the
workspace the run was seeded with and reports which existing files the
requirement is likely to touch, matched by keyword overlap between the
requirement text and each file's path and content. That impact list becomes
both a `Decision` (so the reasoning is in the audit trail) and part of the
design doc a human reviews.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from orchestrator.agents.base import Agent
from orchestrator.contracts import ArtifactKind, NormalizedRequirement, ScenarioKind, Severity

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState

_API_SPEC = """openapi: 3.1.0
info:
  title: URL Shortener API
  version: "1.0"
paths:
  /api/urls:
    post:
      summary: Create a shortened URL
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [long_url]
              properties:
                long_url: {type: string, format: uri}
                custom_alias: {type: string, nullable: true}
                expires_at: {type: string, format: date-time, nullable: true}
      responses:
        "201": {description: created}
        "409": {description: alias already in use}
        "422": {description: invalid long_url}
        "429": {description: rate limited}
  /{code}:
    get:
      summary: Redirect to the original long URL
      parameters:
        - name: code
          in: path
          required: true
          schema: {type: string}
      responses:
        "302": {description: redirect}
        "404": {description: unknown code}
        "410": {description: expired}
  /api/urls/{code}:
    get:
      summary: Get metadata for a short code
      responses:
        "200": {description: ok}
        "404": {description: unknown code}
    delete:
      summary: Revoke a short code
      responses:
        "204": {description: revoked}
        "404": {description: unknown code}
  /api/urls/{code}/stats:
    get:
      summary: Get click analytics for a short code
      responses:
        "200": {description: ok}
        "404": {description: unknown code}
"""

_WORD = re.compile(r"[a-z]{4,}")


class ArchitectureAgent(Agent):
    stage_name = "architecture"

    async def run(self, node: StageNode, state: RunState) -> object:
        raw = state.context.get("normalized_requirement", reader=self.stage_name)
        nreq = NormalizedRequirement.model_validate(raw) if raw else None

        decisions = [
            self.decision(
                "how should short codes be generated?",
                "base62-encoded auto-increment row id, 6+ characters, collision-checked on custom "
                "alias only",
                "monotonic ids avoid a random-collision retry loop on the hot create path; base62 "
                "keeps codes short and URL-safe",
                alternatives=(
                    "random token + collision retry",
                    "hash of the long URL (not unique)",
                ),
                confidence=0.85,
            ),
            self.decision(
                "how is persistence structured?",
                "a single SQLite table behind a repository interface (`Storage` protocol)",
                "the requirement did not mandate a specific database; SQLite needs no external "
                "service for a prototype, and the repository interface is what makes swapping "
                "to Postgres later a config change, not a rewrite",
                alternatives=(
                    "in-memory dict (no durability)",
                    "Postgres (operational overhead unjustified at this stage)",
                ),
                confidence=0.8,
            ),
            self.decision(
                "how is create-endpoint abuse mitigated?",
                "in-memory fixed-window rate limiter, per client IP, applied as ASGI middleware",
                "meets the stated reliability goal without adding an external dependency (Redis); "
                "documented as not distributed-safe -- multiple app instances would each keep "
                "their own counters",
                alternatives=(
                    "Redis-backed limiter (correct under scale-out, adds an operational "
                    "dependency this prototype doesn't need yet)",
                ),
                confidence=0.7,
            ),
        ]

        findings = []
        impact_note = ""
        if state.requirement.kind is ScenarioKind.BROWNFIELD:
            impacted, note = self._codebase_reasoning(state, nreq)
            impact_note = note
            decisions.insert(
                0,
                self.decision(
                    "which existing modules does this change touch?",
                    f"{len(impacted)} file(s): {', '.join(impacted) or 'none matched'}",
                    note,
                    confidence=0.6 if impacted else 0.3,
                ),
            )
            if not impacted:
                findings.append(
                    self.finding(
                        Severity.MEDIUM,
                        "architecture",
                        "no existing file matched the requirement's keywords",
                        detail=(
                            "the brownfield change may need a human to point at the right module"
                        ),
                        remediation="confirm the target module(s) before implementation proceeds",
                    )
                )

        doc = self._render(nreq, decisions, impact_note)
        artifacts = (
            self.artifact("api/openapi.yaml", ArtifactKind.API_SPEC, _API_SPEC),
            self.artifact("docs/architecture.md", ArtifactKind.DOC, doc),
        )

        return self.result(
            summary=f"design finalized: {len(decisions)} architectural decision(s) recorded",
            artifacts=artifacts,
            findings=tuple(findings),
            decisions=tuple(decisions),
            context={"design": {"storage": "sqlite", "code_scheme": "base62-autoincrement"}},
        )

    def _codebase_reasoning(
        self, state: RunState, nreq: NormalizedRequirement | None
    ) -> tuple[list[str], str]:
        """Rank existing workspace files by keyword overlap with the
        requirement. A real static-analysis pass (import graphs, call graphs)
        would do better than lexical overlap, but this is enough to point a
        reviewer at the right module and demonstrates the reasoning runs
        against the actual seeded codebase, not asserted blind.
        """
        terms = set(_WORD.findall(state.requirement.statement.lower()))
        if nreq is not None:
            terms |= set(_WORD.findall(nreq.problem_statement.lower()))
        terms -= {"with", "that", "this", "from", "have", "will", "should"}

        if self.workspace is None:
            return [], "no workspace was seeded for this run (nothing to reason about)"

        scored: list[tuple[int, str, list[str]]] = []
        for path in self.workspace.files():
            path_terms = set(_WORD.findall(path.lower()))
            hits = path_terms & terms
            try:
                content = self.workspace.read(path)
            except (UnicodeDecodeError, OSError):
                content = ""
            content_terms = set(_WORD.findall(content.lower()))
            hits |= content_terms & terms
            if hits:
                scored.append((len(hits), path, sorted(hits)))

        scored.sort(key=lambda t: (-t[0], t[1]))
        top = scored[:8]
        impacted = [path for _, path, _ in top]
        if not impacted:
            return [], f"no existing file matched keywords from the requirement ({sorted(terms)})"

        lines = [f"Matched against requirement keywords {sorted(terms)}:"]
        for _count, path, hits in top:
            lines.append(f"- `{path}` (matched: {', '.join(hits)})")
        return impacted, "\n".join(lines)

    def _render(self, nreq, decisions, impact_note: str) -> str:
        lines = ["# Architecture", ""]
        if impact_note:
            lines += ["## Brownfield impact analysis", impact_note, ""]
        lines += ["## Key decisions", ""]
        for d in decisions:
            lines.append(f"### {d.question}")
            lines.append(f"**Choice:** {d.choice}")
            lines.append(f"**Rationale:** {d.rationale}")
            if d.alternatives:
                lines.append(f"**Alternatives considered:** {', '.join(d.alternatives)}")
            lines.append("")
        lines += [
            "## Layering",
            "- `app/routes.py` -- HTTP layer (FastAPI routers, request/response schemas)",
            "- `app/storage.py` -- persistence (SQLite repository behind a narrow interface)",
            "- `app/codec.py` -- short-code generation (base62 encode/decode)",
            "- `app/middleware.py` -- cross-cutting reliability concerns (rate limiting)",
            "- `app/config.py` -- environment-driven settings",
            "",
            "See `api/openapi.yaml` for the full API contract.",
        ]
        return "\n".join(lines) + "\n"
