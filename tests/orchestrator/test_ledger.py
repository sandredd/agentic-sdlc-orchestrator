from pathlib import Path

from orchestrator.core.ledger import GENESIS, EventType, Ledger


def test_chain_links_and_verifies():
    ledger = Ledger("run_1")
    assert ledger.head == GENESIS

    a = ledger.append(EventType.RUN_STARTED, summary="go")
    b = ledger.append(EventType.STAGE_ENTERED, stage="design")
    c = ledger.append(EventType.STAGE_SUCCEEDED, stage="design")

    assert (a.seq, b.seq, c.seq) == (0, 1, 2)
    assert a.prev_hash == GENESIS
    assert b.prev_hash == a.hash
    assert c.prev_hash == b.hash
    assert ledger.head == c.hash
    assert ledger.verify() == []


def test_tampering_is_detected():
    ledger = Ledger("run_1")
    ledger.append(EventType.RUN_STARTED)
    ledger.append(EventType.STAGE_SUCCEEDED, stage="build", summary="all green")
    ledger.append(EventType.RUN_COMPLETED)

    # Rewrite history: someone edits a stage summary after the fact.
    forged = ledger._events[1].model_copy(update={"summary": "all green (definitely)"})
    ledger._events[1] = forged

    breaks = ledger.verify()
    assert breaks, "a modified event body must break the chain"
    assert breaks[0].seq == 1
    assert "modified" in breaks[0].reason
    # The break propagates: seq 2's prev_hash no longer matches.
    assert any(b.seq == 2 for b in breaks)


def test_dropping_an_event_is_detected():
    ledger = Ledger("run_1")
    for _ in range(4):
        ledger.append(EventType.STAGE_ENTERED, stage="x")
    del ledger._events[2]

    breaks = ledger.verify()
    assert breaks
    assert breaks[0].seq == 2


def test_hash_is_stable_across_reload(tmp_path: Path):
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger("run_7", path=path)
    ledger.append(EventType.RUN_STARTED, payload={"z": 1, "a": [1, 2]})
    ledger.append(EventType.DECISION_RECORDED, stage="arch", payload={"choice": "sqlite"})

    reloaded = Ledger.load("run_7", path)
    assert reloaded.verify() == []
    assert reloaded.head == ledger.head
    assert [e.type for e in reloaded] == [e.type for e in ledger]


def test_queries():
    ledger = Ledger("run_1")
    ledger.append(EventType.STAGE_ENTERED, stage="impl")
    ledger.append(EventType.STAGE_ENTERED, stage="test")
    ledger.append(EventType.STAGE_FAILED, stage="impl")

    assert len(ledger.for_stage("impl")) == 2
    assert len(ledger.of_type(EventType.STAGE_ENTERED)) == 2
    assert len(ledger) == 3


def test_concurrent_appends_are_linearized():
    import threading

    ledger = Ledger("run_1")

    def worker():
        for _ in range(50):
            ledger.append(EventType.ARTIFACT_WRITTEN, stage="impl")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ledger) == 400
    assert ledger.verify() == [], "parallel stages must still yield one intact chain"
    assert [e.seq for e in ledger] == list(range(400))
