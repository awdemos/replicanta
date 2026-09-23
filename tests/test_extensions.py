"""Extensions feature (tier B): the organism's self-patch registry — a
validated, versioned registry of extra learning patterns, utterance seeds
and sentiment vocabulary in artifacts/extensions.json.

By default patches require approval; manual approve/reject is available,
and /auto-apply on allows patches to apply immediately. /revert rolls
back the last applied entry."""

import json
import threading
from typing import ClassVar

from replicanta import extensions, learning, llmclient, sentiment


def _path(tmp_path):
    return tmp_path / "artifacts" / "extensions.json"


def _good_pattern():
    return {
        "kind": "pattern",
        "regex": "i adore ([a-z '-]+)",
        "template": "user:like_{x}:true",
        "example": "i adore hiking",
        "why": "the user says adore",
    }


def test_global_registry_visible_across_threads(tmp_path):
    """The module-level registry is process-global: the web server thread
    and the TUI reflection worker must see what the loading thread loaded
    (pending panel, approved extensions firing)."""
    extensions.reset()
    path_a = tmp_path / "a" / "extensions.json"
    path_b = tmp_path / "b" / "extensions.json"
    extensions.propose(path_a, {"kind": "seed", "text": "a quiet thought"}, auto_apply=True)
    assert path_b.parent.exists() is False  # proposing to A never touches B
    seen = []
    t = threading.Thread(target=lambda: seen.extend(e["text"] for e in extensions.active_entries("seed")))
    t.start()
    t.join()
    assert seen == ["a quiet thought"]
    # A separately-constructed instance still starts empty.
    other_reg = extensions.ExtensionRegistry()
    assert other_reg.active_entries("seed") == []


def test_propose_approve_racing_threads_keep_both_entries(tmp_path):
    """The registry lock covers each mutation end-to-end: hammering
    propose+approve (auto_apply) from threads must not lose an approval."""
    path = _path(tmp_path)
    extensions.reset()

    def worker(i):
        entry = {"kind": "seed", "text": f"seed number {i}"}
        return extensions.propose(path, entry, auto_apply=True)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    texts = {e["text"] for e in extensions.active_entries("seed")}
    assert len(texts) == 8  # no approval lost to a race


def test_voice_state_is_per_thread(monkeypatch):
    from replicanta import llmclient

    llmclient.reset_voice()

    def other():
        return llmclient.voice_online()

    import threading

    result = []
    t = threading.Thread(target=lambda: result.append(other()))
    t.start()
    t.join()
    # Each thread starts with an unknown voice state.
    assert result[0] is None


def test_reset_voice_clears_current_thread():
    from replicanta import llmclient

    llmclient.note_voice_success()
    assert llmclient.voice_online() is True
    llmclient.reset_voice()
    assert llmclient.voice_online() is None


# -- validation ---------------------------------------------------------------


def test_validate_accepts_good_pattern():
    ok, _reason = extensions.validate(_good_pattern())
    assert ok


def test_validate_rejects_bad_regex():
    entry = _good_pattern() | {"regex": "i enjoy (["}
    ok, reason = extensions.validate(entry)
    assert not ok and "compile" in reason


def test_validate_rejects_bad_template():
    entry = _good_pattern() | {"template": "user:like"}
    ok, reason = extensions.validate(entry)
    assert not ok and "template" in reason


def test_validate_rejects_template_outside_belief_vocabulary():
    # a template whose substitution contains digits would crash hear() the
    # first time the pattern fires — it must be refused at staging time
    entry = _good_pattern() | {
        "regex": "i live in zone (.+)",
        "template": "user:zone:9{x}",
        "example": "i live in zone 9lives",
    }
    ok, reason = extensions.validate(entry)
    assert not ok and "vocabulary" in reason


def test_validate_rejects_pattern_not_firing_on_example():
    entry = _good_pattern() | {"example": "the moon is full"}
    ok, reason = extensions.validate(entry)
    assert not ok and "example" in reason


def test_validate_rejects_pattern_firing_on_controls():
    entry = _good_pattern() | {
        "regex": "the weather (.+)",
        "example": "the weather is nice today",
    }
    ok, reason = extensions.validate(entry)
    assert not ok and "unrelated" in reason


def test_validate_rejects_nested_quantifiers():
    # (a+)+-style ambiguity backtracks catastrophically on chat input.
    entry = _good_pattern() | {
        "regex": "i adore (([a-z]+)+)$",
        "example": "i adore hiking",
    }
    ok, reason = extensions.validate(entry)
    assert not ok and "nested quantifiers" in reason


def test_validate_rejects_oversized_pattern():
    entry = _good_pattern() | {"regex": "x" * 201}
    ok, reason = extensions.validate(entry)
    assert not ok and "chars" in reason


def test_validate_seed_and_terms():
    assert extensions.validate({"kind": "seed", "text": "a quiet thought"})[0]
    assert not extensions.validate({"kind": "seed", "text": "x"})[0]
    assert extensions.validate({"kind": "harsh_term", "text": "blork"})[0]
    assert not extensions.validate({"kind": "kind_term", "text": "G00d!"})[0]
    assert not extensions.validate({"kind": "mystery"})[0]


# -- registry round trips -------------------------------------------------------


def test_propose_auto_applies_by_default(tmp_path):
    path = _path(tmp_path)
    applied = extensions.propose(path, _good_pattern(), auto_apply=True)
    assert applied is not None
    assert applied["kind"] == "pattern"
    assert extensions.pending() is None
    assert extensions.registry()["version"] == 1
    assert extensions.active_entries("pattern")[0]["example"] == "i adore hiking"


def test_propose_stages_when_not_auto_apply(tmp_path):
    path = _path(tmp_path)
    extensions.propose(path, _good_pattern(), auto_apply=False)
    assert extensions.pending()["regex"] == "i adore ([a-z '-]+)"
    applied = extensions.approve(path)
    assert applied["kind"] == "pattern"
    assert extensions.pending() is None
    assert extensions.registry()["version"] == 1


def test_reject_clears_pending(tmp_path):
    path = _path(tmp_path)
    extensions.propose(path, _good_pattern(), auto_apply=False)
    rejected = extensions.reject(path)
    assert rejected is not None
    assert extensions.pending() is None
    assert extensions.active_entries("pattern") == []


def test_revert_removes_last_applied(tmp_path):
    path = _path(tmp_path)
    extensions.propose(path, _good_pattern(), auto_apply=False)
    extensions.approve(path)
    extensions.propose(path, {"kind": "seed", "text": "a quiet thought"}, auto_apply=False)
    extensions.approve(path)
    reverted = extensions.revert_last(path)
    assert reverted["kind"] == "seed"
    assert len(extensions.active_entries("pattern")) == 1
    assert extensions.registry()["version"] == 3
    assert extensions.revert_last(path)["kind"] == "pattern"
    assert extensions.revert_last(path) is None


# -- consumers ------------------------------------------------------------------


def test_learning_extract_uses_registry_pattern(tmp_path):
    extensions.load_global(_path(tmp_path))
    extensions.propose(_path(tmp_path), _good_pattern(), auto_apply=True)
    facts = [
        (item["belief"], item["replace"])
        for item in learning.analyze("i adore hiking")["facts"]
        if item["confidence"] >= learning.LEARN_CONF
    ]
    assert (("user", "like_hiking", "true"), False) in facts


def test_sentiment_uses_registry_terms(tmp_path):
    extensions.load_global(_path(tmp_path))
    extensions.propose(_path(tmp_path), {"kind": "harsh_term", "text": "blork"}, auto_apply=True)
    assert sentiment.harshness("you are a blork") > 0.0
    assert sentiment.harshness("you are lovely") == 0.0


def test_seed_pool_uses_registry_seeds(tmp_path):
    from replicanta import narration
    from replicanta.organism import BeliefStore, Lifecycle, Metrics

    class FakeWindow:
        pairs: ClassVar[set] = set()

    class FakeOrg:
        def __init__(self, tmp_path):
            self.store = BeliefStore(tmp_path)
            self.lifecycle = Lifecycle(self.store)
            self.window = FakeWindow()

        def metrics(self):
            return Metrics(self.store)

    extensions.load_global(_path(tmp_path))
    extensions.propose(
        _path(tmp_path),
        {"kind": "seed", "text": "a question about gravity"},
        auto_apply=True,
    )
    import random

    snap = narration.state_snapshot(FakeOrg(tmp_path))
    seeds = {llmclient.seed_for(snap, random.Random(i)) for i in range(80)}
    assert "a question about gravity" in seeds


def test_read_tolerates_corrupt_registry(tmp_path):
    path = tmp_path / "extensions.json"
    path.write_text("{not json")
    assert extensions._read(path) == dict(extensions._EMPTY)


def test_read_normalizes_missing_fields(tmp_path):
    # Valid JSON but no "entries"/"version": consumers (seed_for, approve)
    # used to die with KeyError on every utterance.
    path = tmp_path / "extensions.json"
    path.write_text(json.dumps({"version": 3}))
    assert extensions._read(path) == {"version": 3, "entries": [], "pending": None}


def test_read_defaults_invalid_version_and_entries(tmp_path):
    path = tmp_path / "extensions.json"
    path.write_text(json.dumps({"version": "two", "entries": "not-a-list"}))
    assert extensions._read(path) == dict(extensions._EMPTY)


def test_read_non_object_json_reads_empty(tmp_path):
    path = tmp_path / "extensions.json"
    path.write_text(json.dumps(["a", "list"]))
    assert extensions._read(path) == dict(extensions._EMPTY)


def test_registry_with_missing_fields_does_not_crash_consumers(tmp_path):
    path = tmp_path / "extensions.json"
    path.write_text(json.dumps({"version": 1}))
    extensions.load_global(path)
    assert extensions.active_entries("seed") == []
    assert extensions.pending() is None
    assert extensions.entries() == []
    # approve/seed_for used to raise KeyError here.
    assert extensions.approve(path) is None
    extensions.propose(path, {"kind": "seed", "text": "a quiet thought"}, auto_apply=True)
    assert extensions.active_entries("seed")[0]["text"] == "a quiet thought"


# -- arbiter patch-risk gate --------------------------------------------------------
#
# With ARBITER_URL set, applying a patch is preceded by one batched typed
# decision (risk score + could-break noul). Bands: expectation >= 3.5 is
# critical (blocked), >= 1.5 is medium+ (stays behind explicit /approve
# even when auto-apply is on). Any failure falls back to current behavior.


def _risk_answers(score=0.5, breaks=0.1):
    return {
        "risk": {"type": "score", "score": score, "probabilities": {}, "confidence": 0.9},
        "breaks": {"type": "noul", "noul": breaks, "confidence": 0.9},
    }


def _gate_env(monkeypatch, answers):
    monkeypatch.setenv("ARBITER_URL", "http://localhost:8010")
    monkeypatch.setattr("replicanta.typeddecisions.decide", lambda state, questions: answers)


def test_risk_gate_blocks_critical_patch(tmp_path, monkeypatch):
    _gate_env(monkeypatch, _risk_answers(score=3.9))
    path = _path(tmp_path)
    applied = extensions.propose(path, _good_pattern(), auto_apply=True)
    assert applied is None
    assert extensions.active_entries("pattern") == []  # not applied
    assert extensions.pending() is not None  # left visible for /reject
    # even an explicit /approve refuses and clears the blocked patch
    assert extensions.approve(path) is None
    assert extensions.active_entries("pattern") == []
    assert extensions.pending() is None


def test_risk_gate_blocks_on_break_noul_even_at_low_score(tmp_path, monkeypatch):
    _gate_env(monkeypatch, _risk_answers(score=0.5, breaks=0.9))
    path = _path(tmp_path)
    assert extensions.propose(path, _good_pattern(), auto_apply=True) is None
    assert extensions.active_entries("pattern") == []


def test_risk_gate_confirms_medium_patch_behind_approve(tmp_path, monkeypatch):
    """Medium risk: auto-apply stages instead of applying, and the existing
    /approve flow applies it (the user's confirmation is the gate)."""
    _gate_env(monkeypatch, _risk_answers(score=2.2))
    path = _path(tmp_path)
    applied = extensions.propose(path, _good_pattern(), auto_apply=True)
    assert applied is None
    assert extensions.active_entries("pattern") == []
    assert extensions.pending() is not None
    entry = extensions.approve(path)
    assert entry is not None and entry["kind"] == "pattern"
    assert extensions.active_entries("pattern")[0]["example"] == "i adore hiking"


def test_risk_gate_allows_low_risk_patch(tmp_path, monkeypatch):
    _gate_env(monkeypatch, _risk_answers(score=0.9))
    path = _path(tmp_path)
    applied = extensions.propose(path, _good_pattern(), auto_apply=True)
    assert applied is not None
    assert extensions.active_entries("pattern")[0]["example"] == "i adore hiking"


def test_risk_gate_silent_when_arbiter_down(tmp_path, monkeypatch):
    """decide() -> None keeps the current behavior: auto-apply applies."""
    _gate_env(monkeypatch, None)
    path = _path(tmp_path)
    applied = extensions.propose(path, _good_pattern(), auto_apply=True)
    assert applied is not None
    assert extensions.active_entries("pattern")[0]["example"] == "i adore hiking"


def test_risk_gate_silent_when_arbiter_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("ARBITER_URL", raising=False)

    def forbidden_decide(state, questions):
        raise AssertionError("risk gate must not run without ARBITER_URL")

    monkeypatch.setattr("replicanta.typeddecisions.decide", forbidden_decide)
    path = _path(tmp_path)
    applied = extensions.propose(path, _good_pattern(), auto_apply=True)
    assert applied is not None
    assert extensions.active_entries("pattern")[0]["example"] == "i adore hiking"


def test_validate_rejects_repeated_alternation():
    # (a|a)*b passes a naive "no nested quantifiers" check but backtracks
    # exponentially — alternation inside a repeated group is the shape.
    entry = _good_pattern() | {"regex": "(a|a)*b", "example": "aaab"}
    ok, reason = extensions.validate(entry)
    assert not ok and ("backtrack" in reason or "quantifier" in reason or "alternation" in reason)


def test_validate_rejects_repeated_alternation_variants():
    for pattern in ("(x|y)+z", "(x|y){2,4}z", "((x|y))*z", "(?:x|y)*z"):
        ok, _reason = extensions.validate(_good_pattern() | {"regex": pattern, "example": "xyz"})
        assert not ok, f"{pattern} should be rejected"
    # non-repeated alternation stays legal
    ok, _reason = extensions.validate(
        _good_pattern() | {"regex": "i (adore|love) ([a-z '-]+)", "example": "i adore hiking"}
    )
    assert ok
    # alternation inside a character class is literal, not alternation
    ok, _reason = extensions.validate(_good_pattern() | {"regex": "i adore ([a-z| '-]+)", "example": "i adore hiking"})
    assert ok
