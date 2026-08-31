"""Stage 1: Requirement Understanding.

Turns the raw `Requirement` into a `NormalizedRequirement`: an explicit
problem statement, in/out of scope, functional and non-functional
requirements, acceptance criteria, and -- the part that matters most for the
ambiguous scenario -- a named list of ambiguities, each carrying the
assumption the system will proceed on and whether that assumption is safe to
make unattended (`blocking=False`) or needs a human's actual answer
(`blocking=True`).

Two execution paths, both real:

* With a live provider (:meth:`Agent.think`), the requirement text is handed
  to the model with a schema in the prompt and its structured answer is used
  directly, so requirement understanding actually understands language.
* Without one -- the reproducible default -- a keyword-based heuristic over a
  fixed domain vocabulary (this system targets a URL shortener) fills the
  same schema. It is honest about being a heuristic: every field it produces
  is derived from an explicit, inspectable rule, not invented prose.

Both paths converge on the same `NormalizedRequirement`, which is what makes
the rest of the pipeline indifferent to which one ran.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from orchestrator.agents.base import Agent
from orchestrator.contracts import (
    AcceptanceCriterion,
    Ambiguity,
    ArtifactKind,
    NormalizedRequirement,
    RiskLevel,
    Severity,
)

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState

_SYSTEM = """You are the requirements-analysis stage of an SDLC orchestrator building \
a URL shortener service. Given a raw requirement, produce ONLY a JSON object with keys:
problem_statement (string), in_scope (string[]), out_of_scope (string[]),
functional (string[]), non_functional (string[]),
acceptance (array of {statement, verifiable_by: one of unit|integration|manual|static}),
ambiguities (array of {question, why_it_matters, assumption, confidence: 0-1, blocking: bool}),
risk (one of low|medium|high|critical).
A blocking ambiguity is one where proceeding on any assumption would likely build the wrong \
thing; keep those rare and specific. Respond with JSON only, no prose."""

# Domain vocabulary used by the deterministic fallback to decide what the
# requirement did and did not address. Each tuple is:
#   (keywords that mean "this was specified", assumption text, confidence, functional line)
_ASPECTS: tuple[tuple[tuple[str, ...], str, float, str], ...] = (
    (
        ("custom alias", "vanity", "custom short", "custom slug"),
        "custom aliases are supported as an optional field; a collision is rejected with 409",
        0.75,
        "accept an optional custom alias for a shortened URL",
    ),
    (
        ("expir", "ttl", "time to live"),  # "expir" covers expire/expires/expiring/expiration
        "links do not expire by default; an optional expires_at is accepted and enforced",
        0.75,
        "support an optional expiration time on a shortened URL",
    ),
    (
        ("auth", "api key", "login", "authenticate", "authorization"),
        "no authentication in this prototype; every endpoint is public",
        0.55,
        "expose all endpoints without authentication (prototype scope)",
    ),
    (
        ("analytic", "click count", "stats", "dashboard", "referrer", "geo"),
        "track click_count and last_accessed_at per link; expose them via a stats endpoint",
        0.8,
        "record and expose click analytics per shortened URL",
    ),
    (
        ("database", "postgres", "sqlite", "dynamodb", "redis", "storage"),
        "SQLite backs the prototype behind a repository interface swappable for production scale",
        0.8,
        "persist shortened URLs and their analytics durably",
    ),
    (
        ("rate limit", "throttle", "abuse", "reliability", "resilien"),
        "a simple in-memory fixed-window rate limiter guards the create endpoint per client IP",
        0.6,
        "rate-limit URL creation to protect the service from abuse",
    ),
)

_VAGUE_MARKERS = re.compile(r"\bTBD\b|\bfigure out\b|\bnot sure\b|\?\?|\bsomehow\b", re.IGNORECASE)
_DOMAIN_TERMS = re.compile(
    r"\b(url|link|short|alias|redirect|analytic|click|expire|api|endpoint|"
    r"database|auth|rate|dashboard|stat)\w*",
    re.IGNORECASE,
)


class RequirementsAgent(Agent):
    stage_name = "requirements"

    async def run(self, node: StageNode, state: RunState) -> Any:
        req = state.requirement
        payload = await self.think(
            system=_SYSTEM, prompt=f"Requirement kind: {req.kind.value}\n\n{req.statement}"
        )
        if isinstance(payload, dict) and payload.get("problem_statement"):
            nreq, decisions = self._from_model(req, payload)
        else:
            nreq, decisions = self._heuristic(req)

        doc = self._render(nreq)
        artifact = self.artifact("docs/requirements.md", ArtifactKind.DOC, doc)

        findings = []
        if nreq.blocking_ambiguities:
            for amb in nreq.blocking_ambiguities:
                findings.append(
                    self.finding(
                        Severity.HIGH,
                        "requirements",
                        f"blocking ambiguity: {amb.question}",
                        detail=amb.why_it_matters,
                        remediation="a human must answer this before implementation proceeds",
                    )
                )

        summary = (
            f"normalized into {len(nreq.functional)} functional requirement(s), "
            f"{len(nreq.ambiguities)} ambiguity/ies ({len(nreq.blocking_ambiguities)} blocking), "
            f"risk={nreq.risk.value}"
        )
        return self.result(
            summary=summary,
            artifacts=(artifact,),
            findings=tuple(findings),
            decisions=tuple(decisions),
            context={"normalized_requirement": nreq.model_dump(mode="json")},
        )

    # -- model-backed path ---------------------------------------------------

    def _from_model(
        self, req, payload: dict
    ) -> tuple[NormalizedRequirement, list]:
        ambiguities = tuple(
            Ambiguity(
                question=a.get("question", "unspecified"),
                why_it_matters=a.get("why_it_matters", ""),
                assumption=a.get("assumption", ""),
                confidence=float(a.get("confidence", 0.5)),
                blocking=bool(a.get("blocking", False)),
            )
            for a in payload.get("ambiguities", [])
            if isinstance(a, dict)
        )
        acceptance = tuple(
            AcceptanceCriterion(
                statement=a.get("statement", ""),
                verifiable_by=a.get("verifiable_by", "unit"),
            )
            for a in payload.get("acceptance", [])
            if isinstance(a, dict) and a.get("statement")
        )
        try:
            risk = RiskLevel(payload.get("risk", "medium"))
        except ValueError:
            risk = RiskLevel.MEDIUM

        nreq = NormalizedRequirement(
            source_requirement_id=req.id,
            problem_statement=payload["problem_statement"],
            in_scope=tuple(payload.get("in_scope", ())),
            out_of_scope=tuple(payload.get("out_of_scope", ())),
            functional=tuple(payload.get("functional", ())),
            non_functional=tuple(payload.get("non_functional", ())),
            acceptance=acceptance,
            ambiguities=ambiguities,
            assumptions=tuple(a.assumption for a in ambiguities if not a.blocking),
            risk=risk,
        )
        decisions = [
            self.decision(
                "how should the raw requirement be normalized?",
                f"model-derived problem statement, {len(ambiguities)} ambiguity/ies identified",
                "a live provider was available; its structured reading of the requirement "
                "text is used directly rather than the keyword heuristic",
                confidence=0.75,
            )
        ]
        return nreq, decisions

    # -- deterministic fallback ----------------------------------------------

    def _heuristic(self, req) -> tuple[NormalizedRequirement, list]:
        text = req.statement.lower()
        functional: list[str] = [
            "accept a long URL and return a shortened code",
            "redirect a shortened code to its original long URL",
        ]
        assumptions: list[str] = []
        ambiguities: list[Ambiguity] = []
        decisions = []

        for keywords, assumption, confidence, functional_line in _ASPECTS:
            mentioned = any(k in text for k in keywords)
            functional.append(functional_line)
            if not mentioned:
                assumptions.append(assumption)
                ambiguities.append(
                    Ambiguity(
                        question=f"the requirement does not specify: {functional_line}?",
                        why_it_matters=(
                            "downstream design and implementation need a concrete answer"
                        ),
                        assumption=assumption,
                        confidence=confidence,
                        blocking=False,
                    )
                )
                decisions.append(
                    self.decision(
                        functional_line,
                        assumption,
                        "not addressed in the requirement text; proceeding on a documented, "
                        "low-risk default rather than blocking on it",
                        confidence=confidence,
                    )
                )

        risk = RiskLevel.MEDIUM
        domain_hits = len(set(m.lower() for m in _DOMAIN_TERMS.findall(text)))
        is_vague = bool(_VAGUE_MARKERS.search(req.statement)) or (
            len(text.split()) < 12 and domain_hits <= 2
        )
        if is_vague:
            question = (
                "the request does not name a specific capability to add, change or fix "
                "(e.g. custom aliases, expiration, an analytics dashboard, bulk import, "
                "a particular bug). Which concrete capability should this work target?"
            )
            ambiguities.insert(
                0,
                Ambiguity(
                    question=question,
                    why_it_matters=(
                        "the functional scope of the change is materially different depending "
                        "on the answer; proceeding on a guess risks building the wrong thing"
                    ),
                    assumption=(
                        "none taken -- this requires a human decision before implementation begins"
                    ),
                    confidence=0.2,
                    blocking=True,
                ),
            )
            risk = RiskLevel.HIGH
            decisions.insert(
                0,
                self.decision(
                    "is the requirement specific enough to implement?",
                    "no -- flagged as a blocking ambiguity",
                    f"requirement text has only {domain_hits} recognizable domain term(s) and/or "
                    f"an explicit vagueness marker; a human must clarify the target capability "
                    f"before task decomposition can produce a meaningful plan",
                    confidence=0.9,
                ),
            )

        nreq = NormalizedRequirement(
            source_requirement_id=req.id,
            problem_statement=(
                f"{req.statement.strip()} -- normalized for a URL shortener service "
                f"({req.kind.value} scenario)"
            ),
            in_scope=tuple(functional),
            out_of_scope=(
                "multi-region deployment",
                "a user-facing web dashboard beyond the API",
                "billing or quota enforcement",
            ),
            functional=tuple(functional),
            non_functional=(
                "p99 redirect latency under 100ms on the prototype's local SQLite backend",
                "the create endpoint is rate-limited to reduce abuse",
                "no request is silently dropped: failures return a clear HTTP error",
            ),
            acceptance=(
                AcceptanceCriterion(
                    statement="POST a long URL and receive a working short code",
                    verifiable_by="integration",
                ),
                AcceptanceCriterion(
                    statement="GET the short code redirects (302) to the original long URL",
                    verifiable_by="integration",
                ),
                AcceptanceCriterion(
                    statement=(
                        "the stats endpoint reflects an incremented click_count "
                        "after a redirect"
                    ),
                    verifiable_by="integration",
                ),
            ),
            ambiguities=tuple(ambiguities),
            assumptions=tuple(assumptions),
            risk=risk,
        )
        return nreq, decisions

    # -- rendering ------------------------------------------------------

    def _render(self, nreq: NormalizedRequirement) -> str:
        lines = [
            "# Requirements",
            "",
            f"**Problem statement.** {nreq.problem_statement}",
            "",
            f"**Risk:** {nreq.risk.value}",
            "",
            "## In scope",
            *(f"- {s}" for s in nreq.in_scope),
            "",
            "## Out of scope",
            *(f"- {s}" for s in nreq.out_of_scope),
            "",
            "## Functional requirements",
            *(f"- {s}" for s in nreq.functional),
            "",
            "## Non-functional requirements",
            *(f"- {s}" for s in nreq.non_functional),
            "",
            "## Acceptance criteria",
            *(f"- {a.statement} _(verified by {a.verifiable_by})_" for a in nreq.acceptance),
            "",
            "## Ambiguities and assumptions",
        ]
        if not nreq.ambiguities:
            lines.append("- none identified")
        for amb in nreq.ambiguities:
            marker = "BLOCKING" if amb.blocking else f"assumed (confidence {amb.confidence:.0%})"
            lines.append(f"- **[{marker}]** {amb.question}")
            lines.append(f"  - why it matters: {amb.why_it_matters}")
            lines.append(f"  - assumption: {amb.assumption}")
        return "\n".join(lines) + "\n"
