"""Sentiment scorers: keyword pass plus arbiter typed-decision augmentation.

The keyword lists always run; when ARBITER_URL is set one batched request
scores P(harsh)/P(kind) per utterance and augments the keyword result.
decide() -> None (arbiter down) must fall back to pure keywords, repeats
of the same utterance hit the per-text cache (one request total), and
without ARBITER_URL no request is ever attempted."""

import pytest

from replicanta import sentiment


@pytest.fixture(autouse=True)
def _clear_tone_cache():
    sentiment._tone_cache.clear()
    yield
    sentiment._tone_cache.clear()


def _tone_answers(harsh=None, kind=None):
    answers = {}
    if harsh is not None:
        answers["harsh"] = {"type": "noul", "noul": harsh, "confidence": 0.9}
    if kind is not None:
        answers["kind"] = {"type": "noul", "noul": kind, "confidence": 0.9}
    return answers


def _stub_decide(monkeypatch, answers, calls):
    monkeypatch.setenv("ARBITER_URL", "http://localhost:8010")

    def fake_decide(state, questions):
        calls.append((state, questions))
        return answers

    monkeypatch.setattr("replicanta.typeddecisions.decide", fake_decide)


# -- fallback behavior -------------------------------------------------------------


def test_keywords_alone_when_arbiter_unconfigured(monkeypatch):
    monkeypatch.delenv("ARBITER_URL", raising=False)

    def forbidden_decide(state, questions):
        raise AssertionError("sentiment must not call arbiter without ARBITER_URL")

    monkeypatch.setattr("replicanta.typeddecisions.decide", forbidden_decide)
    assert sentiment.harshness("you are stupid and useless") == 0.06
    assert sentiment.kindness("thank you, friend") == 0.02
    assert sentiment.harshness("the weather is fine") == 0.0
    assert sentiment.kindness("the weather is fine") == 0.0


def test_keyword_fallback_when_decide_returns_none(monkeypatch):
    """Arbiter down: scores come from the keyword lists exactly as before."""
    calls = []
    _stub_decide(monkeypatch, None, calls)
    assert sentiment.harshness("you are stupid and useless") == 0.06
    assert sentiment.kindness("thank you, friend") == 0.02
    assert sentiment.harshness("the weather is fine") == 0.0
    assert calls, "the arbiter was consulted (and failed) before falling back"


def test_keyword_cap_still_applies_with_arbiter(monkeypatch):
    calls = []
    _stub_decide(monkeypatch, _tone_answers(harsh=1.0, kind=1.0), calls)
    assert sentiment.harshness("you stupid useless idiot moron loser trash garbage dumb") == sentiment.HARSHNESS_CAP
    assert sentiment.kindness("good love thank great nice sweet brave") == sentiment.KINDNESS_CAP


# -- arbiter augmentation ------------------------------------------------------------


def test_arbiter_catches_what_keywords_miss(monkeypatch):
    """No keyword hits, but the typed decision reads the message as harsh."""
    calls = []
    _stub_decide(monkeypatch, _tone_answers(harsh=0.8, kind=0.1), calls)
    assert sentiment.harshness("your existence is a rounding error") == pytest.approx(0.8 * sentiment.HARSHNESS_CAP)
    assert sentiment.kindness("your existence is a rounding error") == pytest.approx(0.1 * sentiment.KINDNESS_CAP)


def test_arbiter_augments_keyword_hits(monkeypatch):
    """A keyword hit is never dampened by a calmer arbiter reading."""
    calls = []
    _stub_decide(monkeypatch, _tone_answers(harsh=0.2), calls)
    # two keyword hits give 0.06; the arbiter's 0.2 * CAP (0.03) must not lower it
    assert sentiment.harshness("you are stupid and useless") == 0.06


def test_arbiter_questions_batched_per_utterance(monkeypatch):
    """harshness() + kindness() on the same text is ONE request carrying
    both questions; a second utterance is a second request."""
    calls = []
    _stub_decide(monkeypatch, _tone_answers(harsh=0.5, kind=0.5), calls)
    sentiment.harshness("what do you think")
    sentiment.kindness("what do you think")
    assert len(calls) == 1
    _state, questions = calls[0]
    assert set(questions) == {"harsh", "kind"}
    sentiment.harshness("and another thing entirely")
    assert len(calls) == 2


def test_tone_cache_makes_repeats_free(monkeypatch):
    calls = []
    _stub_decide(monkeypatch, _tone_answers(harsh=0.9), calls)
    first = sentiment.harshness("a peculiar phrase about dust")
    repeat = sentiment.harshness("a peculiar phrase about dust")
    assert first == repeat
    assert len(calls) == 1
