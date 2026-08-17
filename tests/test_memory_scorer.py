"""Hybrid memory ranking: importance + relevance + recall."""

from replicanta.memory import (
    MemoryScorer,
    attach_importance,
    score_importance,
    score_relevance,
)


def test_importance_bounds_and_kind_weights():
    faded = score_importance("faded", "you faded", 10)
    dream = score_importance("dream", "you dreamt", 10)
    assert 0.0 <= faded <= 1.0
    assert 0.0 <= dream <= 1.0
    # faded is a lifecycle event and should score higher than an ordinary dream
    assert faded > dream


def test_relevance_matches_shared_tokens():
    assert score_relevance("the user likes rain", "what does the user like") > 0.0
    assert score_relevance("completely unrelated", "banana rocket") == 0.0


def test_rank_prefers_important_and_relevant():
    memories = [
        {"cycle": 1, "kind": "dream", "text": "a vague dream about clouds"},
        {"cycle": 2, "kind": "harsh", "text": "the user said you are useless"},
        {"cycle": 3, "kind": "learned", "text": "user likes rain"},
    ]
    scorer = MemoryScorer()
    ranked = scorer.rank(memories, "user likes rain", top_k=2, current_cycle=10)
    # The learned memory is both important-ish and directly relevant.
    assert ranked[0]["kind"] == "learned"
    # The harsh memory has high importance but low relevance; learned wins.
    assert len(ranked) == 2


def test_rank_limits_top_k():
    memories = [
        {"cycle": i, "kind": "dream", "text": f"dream {i}"} for i in range(20)
    ]
    scorer = MemoryScorer()
    assert len(scorer.rank(memories, "query", top_k=5)) == 5


def test_mark_recalled_increments_counter():
    m = {"text": "x"}
    MemoryScorer.mark_recalled(m)
    assert m["recall"] == 1
    MemoryScorer.mark_recalled(m)
    assert m["recall"] == 2


def test_attach_importance_fills_missing_fields():
    m = {"kind": "born", "text": "woke into existence", "cycle": 0}
    attach_importance(m, current_cycle=5)
    assert "importance" in m
    assert "recall" in m
    assert m["importance"] > 0.5
