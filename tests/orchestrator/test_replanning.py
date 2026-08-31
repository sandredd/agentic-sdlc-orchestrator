from orchestrator.core.graph import StageGraph
from orchestrator.core.replanning import ReplanHistory, compute_scope

from .conftest import fresh_state, stage


def diamond():
    return StageGraph(
        [stage("a"), stage("b", ("a",)), stage("c", ("a",)), stage("d", ("b", "c"))]
    )


def test_only_stages_that_read_the_changed_key_go_directly_stale():
    graph = diamond()
    state = fresh_state()
    state.context.set("spec", 1, writer="a")
    state.context.get("spec", reader="b")  # c never reads it

    scope = compute_scope(graph, state, ["spec"])
    assert scope.directly_stale == {"b"}


def test_transitive_stale_covers_descendants_of_direct_consumers():
    graph = diamond()
    state = fresh_state()
    state.context.set("spec", 1, writer="a")
    state.context.get("spec", reader="b")

    scope = compute_scope(graph, state, ["spec"])
    assert scope.transitively_stale == {"d"}
    assert scope.stale == {"b", "d"}


def test_sibling_that_never_touched_the_key_stays_out_of_scope():
    graph = diamond()
    state = fresh_state()
    state.context.set("spec", 1, writer="a")
    state.context.get("spec", reader="b")

    scope = compute_scope(graph, state, ["spec"])
    assert "c" not in scope.stale, "c never read the changed key and must not be re-run"


def test_writer_of_the_changed_key_is_not_marked_stale_by_its_own_write():
    graph = diamond()
    state = fresh_state()
    state.context.set("spec", 1, writer="a")
    state.context.get("spec", reader="a")  # pathological self-read, still excluded

    scope = compute_scope(graph, state, ["spec"])
    assert "a" not in scope.stale


def test_empty_scope_when_nobody_consumed_the_key():
    graph = diamond()
    state = fresh_state()
    state.context.set("spec", 1, writer="a")

    scope = compute_scope(graph, state, ["spec"])
    assert bool(scope) is False


def test_multiple_changed_keys_union_their_consumers():
    graph = diamond()
    state = fresh_state()
    state.context.set("spec", 1, writer="a")
    state.context.set("design", 1, writer="a")
    state.context.get("spec", reader="b")
    state.context.get("design", reader="c")

    scope = compute_scope(graph, state, ["spec", "design"])
    assert scope.directly_stale == {"b", "c"}
    assert scope.transitively_stale == {"d"}


# -- history -----------------------------------------------------------


def test_replan_history_tracks_revisions_and_thrash():
    history = ReplanHistory()
    graph = diamond()
    state = fresh_state()
    state.context.set("spec", 1, writer="a")
    state.context.get("spec", reader="b")

    scope1 = compute_scope(graph, state, ["spec"])
    history.record(scope1, triggered_by="human")
    scope2 = compute_scope(graph, state, ["spec"])
    history.record(scope2, triggered_by="human")

    assert history.count == 2
    assert history.thrash_count("b") == 2
    assert history.thrash_count("c") == 0
