"""Stage 3: Task Decomposition.

Converts the normalized requirement's functional list into an ordered,
dependency-aware `Task` graph -- distinct from the SDLC *stage* graph the
engine runs on. The stage graph is fixed (requirements -> architecture ->
implementation -> ...); this is the finer-grained work breakdown *within*
the implementation stage, and it is what a reviewer reads to see that the
requirement was actually decomposed into actionable, sequenced units rather
than treated as one opaque "write the code" step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestrator.agents.base import Agent
from orchestrator.contracts import ArtifactKind, NormalizedRequirement, RiskLevel, Task

if TYPE_CHECKING:
    from orchestrator.core.graph import StageNode
    from orchestrator.core.state import RunState


@dataclass(frozen=True)
class _TaskSpec:
    topic: str  # "core" is unconditional; anything else gates on the functional text
    title: str
    detail: str
    depends_on_keys: tuple[str, ...]  # references to other specs' `key`
    points: int
    key: str


# A capability keyword -> the task it implies, and what it depends on within
# this decomposition. Optional tasks are included only when the normalized
# requirement's functional list actually calls for them, so a narrowly-scoped
# brownfield change gets a narrow plan rather than the full greenfield build.
_TASK_LIBRARY: tuple[_TaskSpec, ...] = (
    _TaskSpec("core", "data model and storage repository",
              "SQLite schema + repository interface for URLs", (), 2, "storage"),
    _TaskSpec("core", "short-code generation",
              "base62 encode/decode with a uniqueness guarantee", ("storage",), 1, "codec"),
    _TaskSpec("core", "create endpoint",
              "POST /api/urls: validate, generate/accept code, persist",
              ("storage", "codec"), 2, "create"),
    _TaskSpec("core", "redirect endpoint",
              "GET /{code}: lookup, increment analytics, 302 or 404/410",
              ("storage",), 2, "redirect"),
    _TaskSpec("alias", "custom alias handling",
              "accept custom_alias, enforce uniqueness, 409 on collision",
              ("storage", "create"), 1, "alias"),
    _TaskSpec("expir", "expiration handling",  # "expir" covers expire/expires/expiration
              "accept/validate expires_at; 410 once passed", ("storage", "redirect"), 1, "expire"),
    _TaskSpec("analytic", "stats endpoint",
              "GET /api/urls/{code}/stats: click_count, last_accessed_at",
              ("storage", "redirect"), 1, "stats"),
    _TaskSpec("rate", "rate limiting middleware",
              "fixed-window limiter on the create endpoint", ("create",), 2, "ratelimit"),
    _TaskSpec("auth", "auth scaffolding note",
              "document the no-auth prototype boundary explicitly", (), 1, "auth_note"),
)


class PlanningAgent(Agent):
    stage_name = "planning"

    async def run(self, node: StageNode, state: RunState) -> object:
        raw = state.context.get("normalized_requirement", reader=self.stage_name)
        nreq = NormalizedRequirement.model_validate(raw) if raw else None
        functional_text = " ".join(nreq.functional).lower() if nreq else ""

        tasks, id_by_key = self._build_tasks(functional_text)

        rationale = (
            "core CRUD/redirect tasks are unconditional; optional tasks (custom alias, "
            "expiration, analytics, rate limiting) are included only when the normalized "
            "functional requirements call for them, so a narrow brownfield change gets a "
            "narrow plan rather than the full greenfield build-out. Storage and code "
            "generation are sequenced first because every endpoint depends on them."
        )

        plan_payload = {
            "normalized_requirement_id": nreq.id if nreq else "unknown",
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "detail": t.detail,
                    "stage": t.stage,
                    "depends_on": list(t.depends_on),
                    "estimate_points": t.estimate_points,
                    "risk": t.risk.value,
                }
                for t in tasks
            ],
            "sequencing_rationale": rationale,
        }
        artifact = self.artifact(
            "docs/plan.json", ArtifactKind.REPORT, json.dumps(plan_payload, indent=2)
        )
        doc_artifact = self.artifact(
            "docs/plan.md", ArtifactKind.DOC, self._render(tasks, rationale)
        )

        decision = self.decision(
            "how is the requirement decomposed into implementation tasks?",
            f"{len(tasks)} task(s), {sum(t.estimate_points for t in tasks)} point(s) total",
            rationale,
            confidence=0.8,
        )

        return self.result(
            summary=f"decomposed into {len(tasks)} task(s) with explicit dependencies",
            artifacts=(artifact, doc_artifact),
            decisions=(decision,),
            context={"plan": plan_payload},
        )

    def _build_tasks(self, functional_text: str) -> tuple[list[Task], dict[str, str]]:
        tasks: list[Task] = []
        id_by_key: dict[str, str] = {}
        for spec in _TASK_LIBRARY:
            if spec.topic != "core" and spec.topic not in functional_text:
                continue
            depends_on = tuple(
                id_by_key[k] for k in spec.depends_on_keys if k in id_by_key
            )
            task = Task(
                title=spec.title,
                detail=spec.detail,
                stage="implementation",
                depends_on=depends_on,
                estimate_points=spec.points,
                risk=RiskLevel.LOW if spec.topic in {"core", "analytic"} else RiskLevel.MEDIUM,
            )
            id_by_key[spec.key] = task.id
            tasks.append(task)
        return tasks, id_by_key

    def _render(self, tasks: list[Task], rationale: str) -> str:
        by_id = {t.id: t for t in tasks}
        lines = [
            "# Task Decomposition", "",
            f"**Sequencing rationale.** {rationale}", "",
            "## Tasks",
        ]
        for t in tasks:
            deps = ", ".join(by_id[d].title for d in t.depends_on if d in by_id) or "none"
            lines.append(f"- **{t.title}** ({t.estimate_points}pt, risk={t.risk.value})")
            lines.append(f"  - {t.detail}")
            lines.append(f"  - depends on: {deps}")
        return "\n".join(lines) + "\n"
