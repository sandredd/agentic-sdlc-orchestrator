from orchestrator.core.state import ContextStore, RunStatus, StageStatus


def test_write_records_provenance():
    ctx = ContextStore()
    assert ctx.set("spec", {"v": 1}, writer="requirements") is True
    assert ctx.writer_of("spec") == "requirements"
    assert ctx.revision("spec") == 1


def test_unchanged_write_is_a_noop_for_lineage():
    ctx = ContextStore()
    ctx.set("spec", {"v": 1}, writer="requirements")
    assert ctx.set("spec", {"v": 1}, writer="requirements") is False, (
        "re-running a deterministic stage must not invalidate everything downstream"
    )
    assert ctx.revision("spec") == 1


def test_changed_write_bumps_revision():
    ctx = ContextStore()
    ctx.set("spec", {"v": 1}, writer="requirements")
    assert ctx.set("spec", {"v": 2}, writer="requirements") is True
    assert ctx.revision("spec") == 2


def test_key_order_does_not_count_as_a_change():
    ctx = ContextStore()
    ctx.set("spec", {"a": 1, "b": 2}, writer="s")
    assert ctx.set("spec", {"b": 2, "a": 1}, writer="s") is False


def test_reads_are_attributed_and_survive_a_rewrite():
    ctx = ContextStore()
    ctx.set("spec", 1, writer="req")
    ctx.get("spec", reader="design")
    ctx.get("spec", reader="impl")
    assert ctx.consumers_of("spec") == {"design", "impl"}

    ctx.set("spec", 2, writer="req")
    assert ctx.consumers_of("spec") == {"design", "impl"}, (
        "consumers of the stale revision are exactly who must be re-planned"
    )


def test_clear_readers_after_requeue():
    ctx = ContextStore()
    ctx.set("spec", 1, writer="req")
    ctx.get("spec", reader="design")
    ctx.clear_readers("spec")
    assert ctx.consumers_of("spec") == set()


def test_missing_key_reads_default_without_recording():
    ctx = ContextStore()
    assert ctx.get("absent", "fallback", reader="x") == "fallback"
    assert ctx.consumers_of("absent") == set()


def test_stage_status_semantics():
    assert StageStatus.SKIPPED.satisfies_dependents, "a bypass must not deadlock the graph"
    assert StageStatus.SUCCEEDED.satisfies_dependents
    assert not StageStatus.FAILED.satisfies_dependents
    assert StageStatus.BLOCKED.terminal
    assert not StageStatus.RUNNING.terminal


def test_run_status_terminality():
    assert RunStatus.HALTED.terminal
    assert not RunStatus.AWAITING_APPROVAL.terminal
