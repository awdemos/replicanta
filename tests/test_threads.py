"""Cognitive thread lifecycle and concurrency."""

import time

import pytest

from replicanta.threads import (
    CognitiveThread,
    ThreadPool,
    derive_in_thread,
    make_self_question_thread,
)


def test_cognitive_thread_defaults_and_validation():
    t = CognitiveThread(id="abc", kind="reflect")
    assert t.status == "pending"
    assert t.payload == {}
    with pytest.raises(ValueError):
        CognitiveThread(id="bad id!", kind="reflect")


def test_thread_pool_harvests_done_futures():
    pool = ThreadPool(max_workers=2)
    try:
        pool.submit("t1", lambda: 42)
        pool.submit("t2", lambda: (_ for _ in ()).throw(ValueError("boom")))
        # wait briefly for the tiny lambdas to finish
        time.sleep(0.05)
        results = pool.harvest()
        assert len(results) == 2
        by_id = {r[0]: (r[1], r[2]) for r in results}
        assert by_id["t1"] == (42, None)
        assert by_id["t2"][1] == "boom"
        assert not pool.pending
    finally:
        pool.shutdown(wait=True)


def test_thread_pool_does_not_harvest_pending():
    pool = ThreadPool(max_workers=2)
    try:
        pool.submit("slow", lambda: time.sleep(0.2) or 1)
        assert pool.harvest() == []
        assert "slow" in pool.pending
    finally:
        pool.shutdown(wait=False)


def test_make_self_question_thread_builds_rule():
    thread, rule, head = make_self_question_thread(
        "color", "blue", "shape", "round", 7, created_cycle=3
    )
    assert thread.kind == "self_question"
    assert thread.created_cycle == 3
    assert head == "q7"
    assert 'bel(x, "color", "blue")' in rule
    assert 'bel(x, "shape", "round")' in rule


def test_derive_in_thread_runs_rule_against_genome():
    genome = 'rel 0.9::bel("cat", "color", "blue")\nrel 0.8::bel("cat", "shape", "round")\n'
    rule = 'q1(x) = bel(x, "color", "blue"), bel(x, "shape", "round")'
    derived = derive_in_thread(genome, rule, "q1")
    assert len(derived) == 1
    tag, tup = derived[0]
    assert tup == ("cat",)
    assert 0.0 <= tag <= 1.0
