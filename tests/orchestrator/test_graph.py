import pytest

from orchestrator.core.graph import (
    GraphValidationError,
    JoinPolicy,
    StageGraph,
    StageNode,
)


def node(name, deps=(), **kw):
    return StageNode(name=name, title=name.title(), depends_on=frozenset(deps), **kw)


def linear_graph():
    return StageGraph([node("a"), node("b", ["a"]), node("c", ["b"])])


def diamond_graph():
    #      a
    #     / \
    #    b   c      (b and c are independent -> parallel)
    #     \ /
    #      d        (ALL join -> synchronization barrier)
    return StageGraph(
        [node("a"), node("b", ["a"]), node("c", ["a"]), node("d", ["b", "c"])]
    )


# -- validation ------------------------------------------------------------


def test_unknown_dependency_is_rejected():
    with pytest.raises(GraphValidationError) as exc:
        StageGraph([node("a", ["ghost"])])
    assert "unknown stage 'ghost'" in str(exc.value)


def test_cycle_is_rejected_and_named():
    with pytest.raises(GraphValidationError) as exc:
        StageGraph([node("a", ["c"]), node("b", ["a"]), node("c", ["b"])])
    assert "dependency cycle" in str(exc.value)


def test_self_dependency_is_rejected():
    with pytest.raises(ValueError, match="cannot depend on itself"):
        node("a", ["a"])


def test_duplicate_names_are_rejected():
    with pytest.raises(GraphValidationError, match="duplicate stage name"):
        StageGraph([node("a"), node("a")])


def test_all_problems_are_reported_not_just_the_first():
    with pytest.raises(GraphValidationError) as exc:
        StageGraph([node("a", ["ghost"]), node("b", ["phantom"])])
    assert len(exc.value.problems) == 2


def test_dataflow_mismatch_is_caught_statically():
    with pytest.raises(GraphValidationError) as exc:
        StageGraph(
            [
                node("design", produces=("design_doc",)),
                node("impl", ["design"], consumes=("test_plan",)),
            ]
        )
    assert "consumes 'test_plan'" in str(exc.value)


def test_dataflow_accepts_transitive_ancestor_production():
    StageGraph(
        [
            node("req", produces=("spec",)),
            node("design", ["req"]),
            node("impl", ["design"], consumes=("spec",)),
        ]
    )


def test_dataflow_rejects_sibling_production():
    # A sibling is not an ancestor: there is no ordering guarantee between them.
    with pytest.raises(GraphValidationError, match="consumes 'notes'"):
        StageGraph(
            [
                node("root"),
                node("left", ["root"], produces=("notes",)),
                node("right", ["root"], consumes=("notes",)),
            ]
        )


# -- structure -------------------------------------------------------------


def test_roots_leaves_and_layers():
    g = diamond_graph()
    assert g.roots() == ("a",)
    assert g.leaves() == ("d",)
    assert g.layers() == [("a",), ("b", "c"), ("d",)]


def test_ancestors_and_descendants():
    g = diamond_graph()
    assert g.ancestors("d") == {"a", "b", "c"}
    assert g.descendants("a") == {"b", "c", "d"}
    assert g.ancestors("a") == set()


def test_topological_order_respects_dependencies():
    order = linear_graph().topological_order()
    assert order == ("a", "b", "c")


# -- scheduling ------------------------------------------------------------


def test_all_join_is_a_synchronisation_barrier():
    g = diamond_graph()
    assert not g.join_satisfied("d", {"a", "b"}), "ALL join must wait for every branch"
    assert g.join_satisfied("d", {"a", "b", "c"})


def test_any_join_unblocks_on_first_dependency():
    g = StageGraph(
        [node("a"), node("b"), node("c", ["a", "b"], join=JoinPolicy.ANY)]
    )
    assert g.join_satisfied("c", {"a"})


def test_ready_returns_only_pending_satisfied_nodes():
    g = diamond_graph()
    ready = g.ready({"a"}, pending={"b", "c", "d"})
    assert ready == {"b", "c"}, "both branches become ready at once -> parallel"


def test_unreachable_propagates_through_all_joins():
    g = diamond_graph()
    # b failed; d has an ALL join, so d can never run.
    assert g.unreachable(satisfied={"a", "c"}, dead={"b"}) == {"d"}


def test_unreachable_spares_any_join_with_a_live_branch():
    g = StageGraph(
        [node("a"), node("b"), node("c", ["a", "b"], join=JoinPolicy.ANY)]
    )
    assert g.unreachable(satisfied=set(), dead={"a"}) == set(), "ANY survives one dead branch"
    assert g.unreachable(satisfied=set(), dead={"a", "b"}) == {"c"}


def test_unreachable_is_transitive():
    g = StageGraph([node("a"), node("b", ["a"]), node("c", ["b"]), node("d", ["c"])])
    assert g.unreachable(satisfied=set(), dead={"a"}) == {"b", "c", "d"}


# -- rendering -------------------------------------------------------------


def test_mermaid_renders_nodes_and_edges():
    out = diamond_graph().to_mermaid(statuses={"a": "succeeded"})
    assert "graph TD" in out
    assert "a --> b" in out
    assert "succeeded" in out


def test_to_dict_is_serialisable():
    import json

    payload = diamond_graph().to_dict()
    assert json.loads(json.dumps(payload))["layers"] == [["a"], ["b", "c"], ["d"]]


def test_optional_node_is_bypassed_not_blocked():
    g = StageGraph(
        [node("a"), node("opt", ["a"], optional=True), node("after", ["opt"])]
    )
    resolution = g.resolve_unreachable(satisfied=set(), dead={"a"})

    assert "opt" in resolution.bypassed
    assert "opt" not in resolution.blocked


def test_optional_bypass_does_not_deadlock_dependents():
    # `after` depends only on the optional stage, so bypassing `opt` must leave
    # `after` live rather than burying it.
    g = StageGraph([node("opt", optional=True), node("after", ["opt"])])
    resolution = g.resolve_unreachable(satisfied=set(), dead=set())
    assert resolution.blocked == set()


def test_non_optional_descendant_of_dead_optional_chain_is_blocked():
    g = StageGraph([node("a"), node("b", ["a"]), node("c", ["b"])])
    resolution = g.resolve_unreachable(satisfied=set(), dead={"a"})
    assert resolution.blocked == {"b", "c"}
    assert resolution.bypassed == set()
