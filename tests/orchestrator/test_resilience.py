import pytest

from orchestrator.config import RetryPolicy
from orchestrator.contracts import StageResult
from orchestrator.core.graph import StageGraph
from orchestrator.core.resilience import (
    ConservativeFallback,
    FailureClass,
    RetryController,
    SkipWithFindingFallback,
    classify,
    plan_rollback,
)

from .conftest import fresh_state, stage

# -- classify ----------------------------------------------------------


@pytest.mark.parametrize(
    "message,expected",
    [
        ("connection reset by peer", FailureClass.TRANSIENT),
        ("request timed out", FailureClass.TRANSIENT),
        ("service temporarily unavailable", FailureClass.TRANSIENT),
        ("429 too many requests", FailureClass.RATE_LIMITED),
        ("rate limit exceeded", FailureClass.RATE_LIMITED),
        ("invalid schema: missing field 'id'", FailureClass.PERMANENT),
        ("", FailureClass.PERMANENT),
    ],
)
def test_classify_from_message(message, expected):
    assert classify(RuntimeError(message)) is expected


def test_classify_timeout_error_instance():
    assert classify(TimeoutError()) is FailureClass.TRANSIENT


# -- RetryController -----------------------------------------------------


def test_permanent_and_policy_failures_are_never_retried():
    controller = RetryController(RetryPolicy(max_attempts=5))
    node = stage("a")
    assert controller.decide(node, 1, FailureClass.PERMANENT).should_retry is False
    assert controller.decide(node, 1, FailureClass.POLICY).should_retry is False


def test_transient_failure_retries_until_budget_exhausted():
    controller = RetryController(RetryPolicy(max_attempts=3))
    node = stage("a")
    first = controller.decide(node, 1, FailureClass.TRANSIENT)
    second = controller.decide(node, 2, FailureClass.TRANSIENT)
    third = controller.decide(node, 3, FailureClass.TRANSIENT)
    assert first.should_retry and second.should_retry
    assert third.should_retry is False
    assert "exhausted" in third.reason


def test_node_level_max_attempts_overrides_default():
    controller = RetryController(RetryPolicy(max_attempts=10))
    node = stage("a", max_attempts=1)
    assert controller.decide(node, 1, FailureClass.TRANSIENT).should_retry is False


def test_rate_limited_uses_max_backoff_floor():
    policy = RetryPolicy(max_attempts=5, backoff_seconds=0.01, max_backoff_seconds=5.0)
    controller = RetryController(policy)
    decision = controller.decide(stage("a"), 1, FailureClass.RATE_LIMITED)
    assert decision.delay_seconds == 5.0


# -- fallbacks ------------------------------------------------------------


async def test_conservative_fallback_tags_the_summary():
    async def simple(node, state):
        return StageResult(stage=node.name, summary="did the simple thing")

    fb = ConservativeFallback(simple)
    result = await fb.execute(stage("impl"), fresh_state())
    assert "fallback:conservative" in result.summary
    assert "did the simple thing" in result.summary


async def test_skip_with_finding_fallback_produces_a_medium_finding():
    fb = SkipWithFindingFallback()
    result = await fb.execute(stage("docs"), fresh_state())
    assert len(result.findings) == 1
    assert result.findings[0].category == "reliability"


# -- rollback planning ------------------------------------------------------


def test_plan_rollback_includes_explicit_coupling():
    graph = StageGraph(
        [
            stage("migrate", rollback_with=frozenset({"seed"})),
            stage("seed", ("migrate",)),
        ]
    )
    plan = plan_rollback(graph, "migrate", ran={"migrate", "seed"})
    assert plan.stages == ("seed",)


def test_plan_rollback_includes_already_run_descendants():
    graph = StageGraph([stage("a"), stage("b", ("a",)), stage("c", ("b",))])
    plan = plan_rollback(graph, "a", ran={"a", "b", "c"})
    assert set(plan.stages) == {"b", "c"}


def test_plan_rollback_excludes_descendants_that_never_ran():
    graph = StageGraph([stage("a"), stage("b", ("a",))])
    plan = plan_rollback(graph, "a", ran={"a"})
    assert plan.stages == ()
    assert bool(plan) is False


def test_plan_rollback_excludes_coupled_stage_that_never_ran():
    graph = StageGraph(
        [stage("migrate", rollback_with=frozenset({"seed"})), stage("seed", ("migrate",))]
    )
    plan = plan_rollback(graph, "migrate", ran={"migrate"})
    assert plan.stages == ()
