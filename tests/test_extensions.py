"""Extensions feature (tier B): the organism's approval-gated genome
patches — a validated, versioned registry of extra learning patterns,
utterance seeds and sentiment vocabulary in artifacts/extensions.json.
Nothing applies without /approve; /revert rolls back."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import extensions
import learning
import sentiment


def _path(tmp_path):
    return tmp_path / "artifacts" / "extensions.json"


def _good_pattern():
    return {"kind": "pattern", "regex": "i adore ([a-z '-]+)",
            "template": "user:like_{x}:true", "example": "i adore hiking",
            "why": "the user says adore"}


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


def test_validate_rejects_pattern_not_firing_on_example():
    entry = _good_pattern() | {"example": "the moon is full"}
    ok, reason = extensions.validate(entry)
    assert not ok and "example" in reason


def test_validate_rejects_pattern_firing_on_controls():
    entry = _good_pattern() | {"regex": "the weather (.+)",
                               "example": "the weather is nice today"}
    ok, reason = extensions.validate(entry)
    assert not ok and "unrelated" in reason


def test_validate_seed_and_terms():
    assert extensions.validate({"kind": "seed", "text": "a quiet thought"})[0]
    assert not extensions.validate({"kind": "seed", "text": "x"})[0]
    assert extensions.validate({"kind": "harsh_term", "text": "blork"})[0]
    assert not extensions.validate({"kind": "kind_term", "text": "G00d!"})[0]
    assert not extensions.validate({"kind": "mystery"})[0]


# -- registry round trips -------------------------------------------------------

def test_propose_approve_flow(tmp_path):
    path = _path(tmp_path)
    extensions.propose(path, _good_pattern())
    assert extensions.pending()["regex"] == "i adore ([a-z '-]+)"
    applied = extensions.approve(path)
    assert applied["kind"] == "pattern"
    assert extensions.pending() is None
    assert extensions.registry()["version"] == 1
    assert extensions.active_entries("pattern")[0]["example"] == "i adore hiking"


def test_reject_clears_pending(tmp_path):
    path = _path(tmp_path)
    extensions.propose(path, _good_pattern())
    rejected = extensions.reject(path)
    assert rejected is not None
    assert extensions.pending() is None
    assert extensions.active_entries("pattern") == []


def test_revert_removes_last_applied(tmp_path):
    path = _path(tmp_path)
    extensions.propose(path, _good_pattern())
    extensions.approve(path)
    extensions.propose(path, {"kind": "seed", "text": "a quiet thought"})
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
    extensions.propose(_path(tmp_path), _good_pattern())
    extensions.approve(_path(tmp_path))
    facts = learning.extract("i adore hiking")
    assert (("user", "like_hiking", "true"), False) in facts


def test_sentiment_uses_registry_terms(tmp_path):
    extensions.load_global(_path(tmp_path))
    extensions.propose(_path(tmp_path),
                       {"kind": "harsh_term", "text": "blork"})
    extensions.approve(_path(tmp_path))
    assert sentiment.harshness("you are a blork") > 0.0
    assert sentiment.harshness("you are lovely") == 0.0


def test_seed_pool_uses_registry_seeds(tmp_path):
    import narration
    from organism import BeliefStore, Lifecycle, Metrics

    class FakeWindow:
        pairs = set()

    class FakeOrg:
        def __init__(self, tmp_path):
            self.store = BeliefStore(tmp_path)
            self.lifecycle = Lifecycle(self.store)
            self.window = FakeWindow()

        def metrics(self):
            return Metrics(self.store)

    extensions.load_global(_path(tmp_path))
    extensions.propose(_path(tmp_path),
                       {"kind": "seed", "text": "a question about gravity"})
    extensions.approve(_path(tmp_path))
    import random
    snap = narration.state_snapshot(FakeOrg(tmp_path))
    seeds = {narration._seed_for(snap, random.Random(i)) for i in range(80)}
    assert "a question about gravity" in seeds
