"""Wires the nine SDLC stage agents onto the engine's dependency graph.

`build_graph()` returns the same shape shown throughout the design docs:

    requirements -+- architecture -+
                  \\- planning -----+- implementation -+- testing --+
                                                        +- security -+- validation - release
                                                        \\- docs ----+

`build_agents()` returns the matching `StageExecutor` for each node, bound
to one provider (and, for stages that need it, the run's workspace) so the
engine can dispatch by name without knowing which concrete agent class is
behind it.
"""

from __future__ import annotations

from orchestrator.agents.architecture import ArchitectureAgent
from orchestrator.agents.base import Agent
from orchestrator.agents.docs import DocsAgent
from orchestrator.agents.implementation import ImplementationAgent
from orchestrator.agents.planning import PlanningAgent
from orchestrator.agents.release import ReleaseAgent
from orchestrator.agents.requirements import RequirementsAgent
from orchestrator.agents.security import SecurityAgent
from orchestrator.agents.testing import TestingAgent
from orchestrator.agents.validation import ValidationAgent
from orchestrator.core.gates import (
    ArtifactsProducedGate,
    DecisionsRecordedGate,
    NoBlockingAmbiguityGate,
    PromisedOutputGate,
    RequiredContextGate,
    SeverityGate,
    UpstreamCleanGate,
)
from orchestrator.core.graph import ApprovalPoint, JoinPolicy, StageGraph, StageNode
from orchestrator.core.resilience import ConservativeFallback
from orchestrator.providers.base import Provider
from orchestrator.providers.deterministic import DeterministicProvider


def build_graph() -> StageGraph:
    return StageGraph(
        [
            StageNode(
                name="requirements",
                title="Requirement Understanding",
                produces=("normalized_requirement",),
                exit_gates=(PromisedOutputGate(), DecisionsRecordedGate()),
            ),
            StageNode(
                name="architecture",
                title="Architecture & Design",
                depends_on=frozenset({"requirements"}),
                consumes=("normalized_requirement",),
                produces=("design",),
                entry_gates=(RequiredContextGate(), NoBlockingAmbiguityGate()),
                exit_gates=(PromisedOutputGate(), DecisionsRecordedGate()),
            ),
            StageNode(
                name="planning",
                title="Task Decomposition",
                depends_on=frozenset({"requirements"}),
                consumes=("normalized_requirement",),
                produces=("plan",),
                entry_gates=(RequiredContextGate(), NoBlockingAmbiguityGate()),
                exit_gates=(PromisedOutputGate(), DecisionsRecordedGate()),
            ),
            StageNode(
                name="implementation",
                title="Implementation",
                depends_on=frozenset({"architecture", "planning"}),
                consumes=("design", "plan"),
                produces=("code",),
                entry_gates=(RequiredContextGate(),),
                exit_gates=(ArtifactsProducedGate(minimum=1, kinds=("code",)),),
                rollback_with=frozenset({"testing", "security", "docs"}),
            ),
            StageNode(
                name="testing",
                title="Test Generation & Execution",
                depends_on=frozenset({"implementation"}),
                consumes=("code",),
                produces=("test_report",),
                entry_gates=(RequiredContextGate(),),
                exit_gates=(ArtifactsProducedGate(minimum=1, kinds=("test",)),),
                critical=False,
                fallback=None,  # a red suite is a finding, not a crash -- nothing to fall back to
            ),
            StageNode(
                name="security",
                title="Security Review",
                depends_on=frozenset({"implementation"}),
                consumes=("code",),
                produces=("security_report",),
                entry_gates=(RequiredContextGate(),),
                exit_gates=(PromisedOutputGate(),),
                critical=False,
            ),
            StageNode(
                name="docs",
                title="Documentation",
                depends_on=frozenset({"implementation"}),
                consumes=("code",),
                optional=True,
                critical=False,
            ),
            StageNode(
                name="validation",
                title="Validation and Risk Control",
                depends_on=frozenset({"testing", "security", "docs"}),
                join=JoinPolicy.ALL,
                consumes=(),
                exit_gates=(DecisionsRecordedGate(),),
            ),
            StageNode(
                name="release",
                title="Release Readiness",
                depends_on=frozenset({"validation"}),
                entry_gates=(UpstreamCleanGate("validation"),),
                exit_gates=(
                    SeverityGate(),  # default ceiling HIGH: a BLOCKER finding halts release
                    DecisionsRecordedGate(),
                ),
                high_impact=True,
                approval_point=ApprovalPoint.EXIT,
            ),
        ]
    )


def build_agents(
    provider: Provider | None = None, *, workspace=None
) -> dict[str, Agent]:
    provider = provider or DeterministicProvider()
    return {
        "requirements": RequirementsAgent(provider),
        "architecture": ArchitectureAgent(provider, workspace=workspace),
        "planning": PlanningAgent(provider),
        "implementation": ImplementationAgent(provider),
        "testing": TestingAgent(provider, workspace=workspace),
        "security": SecurityAgent(provider),
        "docs": DocsAgent(provider),
        "validation": ValidationAgent(provider),
        "release": ReleaseAgent(provider),
    }


def make_executor(provider: Provider | None = None, *, workspace=None):
    """Bridges the agent registry to the engine's single-callable
    `StageExecutor` contract: the engine only knows how to call one function
    per node name, so this dispatches to the bound agent for that stage."""
    agents = build_agents(provider, workspace=workspace)

    async def executor(node: StageNode, state):
        return await agents[node.name](node, state)

    return executor


__all__ = [
    "ConservativeFallback",
    "build_agents",
    "build_graph",
    "make_executor",
]
