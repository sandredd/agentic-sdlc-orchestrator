"""Run configuration and autonomy boundaries.

The autonomy boundary is expressed as data, not as scattered `if` statements:
:class:`AutonomyLevel` says how far agents may go unattended, and
:class:`OrchestratorConfig` says what the engine does when they reach the edge.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.contracts import RiskLevel


class AutonomyLevel(StrEnum):
    """How much the agents may do without a human in the loop."""

    SUGGEST = "suggest"          # propose everything, execute nothing
    SUPERVISED = "supervised"    # execute, but every stage exit needs approval
    BOUNDED = "bounded"          # execute freely below the risk threshold
    AUTONOMOUS = "autonomous"    # execute freely; humans review after the fact

    @property
    def rank(self) -> int:
        return {"suggest": 0, "supervised": 1, "bounded": 2, "autonomous": 3}[self.value]


class RetryPolicy(BaseModel):
    """Bounded retry with exponential backoff.

    `max_attempts` counts the *total* tries including the first, so 1 means
    "no retry". Backoff is capped so a long-tailed failure cannot stall a run
    past its deadline.
    """

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_seconds: float = Field(default=0.5, ge=0.0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)
    max_backoff_seconds: float = Field(default=8.0, ge=0.0)

    def delay_for(self, attempt: int) -> float:
        """Delay *before* the given 1-indexed attempt."""
        if attempt <= 1:
            return 0.0
        raw = self.backoff_seconds * (self.backoff_multiplier ** (attempt - 2))
        return min(raw, self.max_backoff_seconds)


class OrchestratorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_root: Path = Path(".asdlc/runs")
    autonomy: AutonomyLevel = AutonomyLevel.BOUNDED

    # Risk at or above this level always escalates to a human, regardless of
    # autonomy level. This is the hard floor on controlled autonomy.
    approval_risk_threshold: RiskLevel = RiskLevel.HIGH

    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    # Wall-clock ceiling for a whole run; the engine safe-stops past it.
    run_deadline_seconds: float = Field(default=900.0, gt=0)
    stage_timeout_seconds: float = Field(default=120.0, gt=0)

    # A stage that keeps triggering upstream re-planning is a sign of an
    # unstable requirement, not a flaky agent. Cap it so runs terminate.
    max_replans: int = Field(default=2, ge=0, le=10)

    max_parallel_stages: int = Field(default=4, ge=1, le=32)

    provider: str = Field(default="deterministic")
    model: str = Field(default="claude-sonnet-5")

    fail_fast: bool = False

    @classmethod
    def from_env(cls, **overrides: object) -> OrchestratorConfig:
        env: dict[str, object] = {}
        if os.getenv("ANTHROPIC_API_KEY"):
            env["provider"] = "anthropic"
        if model := os.getenv("ASDLC_MODEL"):
            env["model"] = model
        if autonomy := os.getenv("ASDLC_AUTONOMY"):
            env["autonomy"] = AutonomyLevel(autonomy)
        if root := os.getenv("ASDLC_RUN_ROOT"):
            env["run_root"] = Path(root)
        return cls(**{**env, **overrides})
