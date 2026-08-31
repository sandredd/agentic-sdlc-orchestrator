"""The explicit SDLC dependency graph.

The orchestration is a DAG, not a chain. Concretely that buys three things the
assessment asks for:

* **Parallel paths with synchronization.** Independent branches (say, security
  review and documentation) dispatch as soon as their own dependencies are met;
  a downstream node with :attr:`JoinPolicy.ALL` is a sync barrier that waits for
  every inbound branch.
* **Non-linear execution.** The engine never walks topological layers in
  lockstep. It maintains a frontier and dispatches each node the instant its
  join condition is satisfied, so a fast branch never waits on a slow sibling.
* **Static verifiability.** Cycles, dangling dependencies and *data* wiring
  errors are caught at graph-construction time, before an agent burns a token.

The data check is worth spelling out: nodes declare the context keys they
``consume`` and ``produce``. If a stage consumes a key no ancestor produces,
that is a mis-wired pipeline, and it is far cheaper to fail there than to
discover it as a ``None`` three stages downstream.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from orchestrator.core.approvals import ApprovalPoint

if TYPE_CHECKING:
    from orchestrator.core.gates import EntryGate, ExitGate
    from orchestrator.core.resilience import FallbackStrategy


class GraphValidationError(ValueError):
    """Raised with *every* structural problem found, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(f"stage graph is invalid:\n  - {joined}")


@dataclass(frozen=True)
class Unreachable:
    """Nodes that cannot run, split by how the graph wants them handled."""

    blocked: set[str]   # hard-blocked: a required dependency will never satisfy
    bypassed: set[str]  # optional: skipped without blocking dependents


class JoinPolicy(StrEnum):
    ALL = "all"  # synchronization barrier: every dependency must be satisfied
    ANY = "any"  # first satisfied dependency unblocks this node


@dataclass(frozen=True)
class StageNode:
    """One SDLC stage.

    ``critical`` and ``optional`` are deliberately distinct. ``optional`` means
    the stage may be skipped without breaking the graph behind it; ``critical``
    means a *failure* of this stage halts the run rather than merely blocking
    its descendants.
    """

    name: str
    title: str
    depends_on: frozenset[str] = field(default_factory=frozenset)
    join: JoinPolicy = JoinPolicy.ALL
    optional: bool = False
    critical: bool = True
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    entry_gates: tuple[EntryGate, ...] = ()
    exit_gates: tuple[ExitGate, ...] = ()
    # Stages that must be rolled back with this one if it is reverted.
    rollback_with: frozenset[str] = field(default_factory=frozenset)

    # Governance. `high_impact` forces a human checkpoint regardless of the
    # configured autonomy level -- raising autonomy must not be able to switch
    # off the gate on a release.
    high_impact: bool = False
    approval_point: ApprovalPoint = ApprovalPoint.EXIT
    max_attempts: int | None = None      # overrides the global retry budget
    fallback: FallbackStrategy | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("stage node requires a name")
        if self.name in self.depends_on:
            raise ValueError(f"stage {self.name!r} cannot depend on itself")


class StageGraph:
    """An immutable, validated DAG of stages."""

    def __init__(self, nodes: list[StageNode]) -> None:
        self._nodes: dict[str, StageNode] = {}
        problems: list[str] = []

        for node in nodes:
            if node.name in self._nodes:
                problems.append(f"duplicate stage name {node.name!r}")
            self._nodes[node.name] = node

        problems.extend(self._check_dependencies())
        if cycle := self._find_cycle():
            problems.append(f"dependency cycle: {' -> '.join(cycle)}")
        else:
            # Data-flow validation needs a usable topology; skip it if the
            # graph is cyclic, since ancestry is undefined there.
            problems.extend(self._check_dataflow())

        if problems:
            raise GraphValidationError(problems)

        self._children: dict[str, frozenset[str]] = {
            name: frozenset(n.name for n in self._nodes.values() if name in n.depends_on)
            for name in self._nodes
        }

    # -- validation --------------------------------------------------------

    def _check_dependencies(self) -> list[str]:
        problems = []
        for node in self._nodes.values():
            for dep in sorted(node.depends_on):
                if dep not in self._nodes:
                    problems.append(f"stage {node.name!r} depends on unknown stage {dep!r}")
        return problems

    def _find_cycle(self) -> list[str] | None:
        """Kahn's algorithm; on failure, walk the residual to name the cycle."""
        indegree = {
            name: sum(1 for d in node.depends_on if d in self._nodes)
            for name, node in self._nodes.items()
        }
        queue = deque(sorted(n for n, d in indegree.items() if d == 0))
        seen = 0
        while queue:
            current = queue.popleft()
            seen += 1
            for name, node in sorted(self._nodes.items()):
                if current in node.depends_on:
                    indegree[name] -= 1
                    if indegree[name] == 0:
                        queue.append(name)
        if seen == len(self._nodes):
            return None

        residual = {n for n, d in indegree.items() if d > 0}
        start = sorted(residual)[0]
        path = [start]
        current = start
        while True:
            nxt = next(
                (d for d in sorted(self._nodes[current].depends_on) if d in residual), None
            )
            if nxt is None:
                return path
            if nxt in path:
                return [*path[path.index(nxt):], nxt]
            path.append(nxt)
            current = nxt

    def _check_dataflow(self) -> list[str]:
        problems = []
        for node in self._nodes.values():
            available = {
                key
                for ancestor in self._ancestors_unchecked(node.name)
                for key in self._nodes[ancestor].produces
            }
            for key in node.consumes:
                if key not in available:
                    problems.append(
                        f"stage {node.name!r} consumes {key!r}, "
                        f"which no upstream stage produces"
                    )
        return problems

    def _ancestors_unchecked(self, name: str) -> set[str]:
        found: set[str] = set()
        queue = deque(self._nodes[name].depends_on)
        while queue:
            current = queue.popleft()
            if current in found or current not in self._nodes:
                continue
            found.add(current)
            queue.extend(self._nodes[current].depends_on)
        return found

    # -- structure ---------------------------------------------------------

    def __contains__(self, name: object) -> bool:
        return name in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def __iter__(self):
        return iter(self._nodes.values())

    def __getitem__(self, name: str) -> StageNode:
        return self._nodes[name]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._nodes)

    def node(self, name: str) -> StageNode:
        return self._nodes[name]

    def children(self, name: str) -> frozenset[str]:
        return self._children[name]

    def ancestors(self, name: str) -> set[str]:
        return self._ancestors_unchecked(name)

    def descendants(self, name: str) -> set[str]:
        found: set[str] = set()
        queue = deque(self._children[name])
        while queue:
            current = queue.popleft()
            if current in found:
                continue
            found.add(current)
            queue.extend(self._children[current])
        return found

    def roots(self) -> tuple[str, ...]:
        return tuple(n for n, node in self._nodes.items() if not node.depends_on)

    def leaves(self) -> tuple[str, ...]:
        return tuple(n for n in self._nodes if not self._children[n])

    def layers(self) -> list[tuple[str, ...]]:
        """Topological levels. Used for *rendering*, never for scheduling —
        the engine dispatches on the frontier, which is strictly faster."""
        remaining = {n: set(node.depends_on) for n, node in self._nodes.items()}
        out: list[tuple[str, ...]] = []
        while remaining:
            layer = tuple(sorted(n for n, deps in remaining.items() if not deps))
            if not layer:  # unreachable: construction rejects cycles
                raise GraphValidationError(["cycle detected while layering"])
            out.append(layer)
            for name in layer:
                del remaining[name]
            for deps in remaining.values():
                deps.difference_update(layer)
        return out

    def topological_order(self) -> tuple[str, ...]:
        return tuple(name for layer in self.layers() for name in layer)

    # -- scheduling --------------------------------------------------------

    def join_satisfied(self, name: str, satisfied: set[str]) -> bool:
        node = self._nodes[name]
        if not node.depends_on:
            return True
        if node.join is JoinPolicy.ANY:
            return bool(node.depends_on & satisfied)
        return node.depends_on <= satisfied

    def ready(self, satisfied: set[str], *, pending: set[str]) -> set[str]:
        """Nodes whose join condition is met and which have not run yet."""
        return {n for n in pending if self.join_satisfied(n, satisfied)}

    def resolve_unreachable(self, satisfied: set[str], dead: set[str]) -> Unreachable:
        """Fixed-point classification of nodes that can never become ready.

        Two reasons this is computed iteratively rather than as the descendant
        closure of a failure:

        * a ``JoinPolicy.ANY`` node survives as long as *one* inbound edge is
          still alive, and a plain closure would wrongly bury it;
        * an ``optional`` node that cannot run is **bypassed**, not blocked.
          The graph declared that the pipeline works without it, so it must not
          deadlock everything behind it. Its descendants stay live and are
          caught later by :class:`RequiredContextGate` if they genuinely needed
          an output that was never produced — a precise runtime failure beats a
          blanket structural one.
        """
        blocked = set(dead)
        bypassed: set[str] = set()
        live = set(satisfied)
        changed = True
        while changed:
            changed = False
            for name, node in self._nodes.items():
                if name in blocked or name in bypassed or name in live or not node.depends_on:
                    continue
                if node.join is JoinPolicy.ANY:
                    doomed = node.depends_on <= blocked
                else:
                    doomed = bool(node.depends_on & blocked)
                if not doomed:
                    continue
                if node.optional:
                    bypassed.add(name)
                    live.add(name)
                else:
                    blocked.add(name)
                changed = True
        return Unreachable(blocked=blocked - set(dead), bypassed=bypassed)

    def unreachable(self, satisfied: set[str], dead: set[str]) -> set[str]:
        """Convenience view: only the hard-blocked nodes."""
        return self.resolve_unreachable(satisfied, dead).blocked

    # -- rendering ---------------------------------------------------------

    def to_mermaid(self, statuses: dict[str, str] | None = None) -> str:
        statuses = statuses or {}
        lines = ["graph TD"]
        for name, node in self._nodes.items():
            label = node.title
            if status := statuses.get(name):
                label = f"{label}<br/><i>{status}</i>"
            shape = f'{name}(["{label}"])' if node.optional else f'{name}["{label}"]'
            lines.append(f"    {shape}")
        for name, node in self._nodes.items():
            for dep in sorted(node.depends_on):
                arrow = "-.->" if node.join is JoinPolicy.ANY else "-->"
                lines.append(f"    {dep} {arrow} {name}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        """Serializable structure (gates by name) for reports and the API."""
        return {
            "nodes": [
                {
                    "name": n.name,
                    "title": n.title,
                    "depends_on": sorted(n.depends_on),
                    "join": n.join.value,
                    "optional": n.optional,
                    "critical": n.critical,
                    "consumes": list(n.consumes),
                    "produces": list(n.produces),
                    "entry_gates": [g.name for g in n.entry_gates],
                    "exit_gates": [g.name for g in n.exit_gates],
                }
                for n in self._nodes.values()
            ],
            "layers": [list(layer) for layer in self.layers()],
        }
