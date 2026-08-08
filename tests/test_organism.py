import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from organism import Mind

SCL = Path(__file__).parent.parent / "organism.scl"


def test_mind_loads_seed_and_reads_beliefs():
    mind = Mind(SCL)
    mind.rebuild()
    beliefs = mind.beliefs()
    assert ("self", "color", "blue") in beliefs
    assert ("self", "shape", "round") in beliefs
    assert beliefs[("self", "color", "blue")] > 0.5


def test_mind_beliefs_returns_float_confidences():
    mind = Mind(SCL)
    mind.rebuild()
    for conf in mind.beliefs().values():
        assert isinstance(conf, float)
        assert 0.0 <= conf <= 1.0

from organism import BeliefStore, VALID_VALUE_RE  # noqa: F401
import pytest

@pytest.fixture
def store(tmp_path):
    return BeliefStore(tmp_path)

def test_add_new_belief(store):
    store.add(("apple", "color", "red"), 0.8)
    assert store.conf(("apple", "color", "red")) == 0.8

def test_strengthen_keeps_max(store):
    store.add(("apple", "color", "red"), 0.6)
    store.add(("apple", "color", "red"), 0.9)
    assert store.conf(("apple", "color", "red")) == 0.9

def test_contradiction_prunes_lower_confidence(store):
    store.add(("apple", "color", "red"), 0.9)
    store.add(("apple", "color", "green"), 0.6)
    assert store.conf(("apple", "color", "red")) == 0.9
    assert ("apple", "color", "green") not in store.beliefs()
    assert ("apple", "color", "green") in store.archived()

def test_invalid_value_rejected(store):
    with pytest.raises(ValueError):
        store.add(("apple", "color", "Not Valid!"), 0.9)

def test_render_scl_matches_import_file_format(store):
    store.add(("apple", "color", "red"), 0.9)
    scl = store.render_scl()
    assert 'rel 0.9::bel("apple", "color", "red")' in scl

def test_save_load_roundtrip(store):
    store.add(("apple", "color", "red"), 0.9)
    store.chaos = 0.7
    store.save()
    loaded = BeliefStore(store.dir_path)
    loaded.load()
    assert loaded.conf(("apple", "color", "red")) == 0.9
    assert loaded.chaos == 0.7

from organism import ChaosKnob, AttentionWindow

def test_chaos_knob_clamps():
    knob = ChaosKnob()
    knob.set(1.5)
    assert knob.value == 1.0
    knob.set(-0.2)
    assert knob.value == 0.0
    knob.set(0.7)
    assert knob.value == 0.7

def test_attention_window_from_beliefs():
    beliefs = {("apple", "color", "red"): 0.9, ("ball", "shape", "round"): 0.8}
    win = AttentionWindow(beliefs)
    win.refresh()
    assert ("color", "red") in win.pairs
    assert ("shape", "round") in win.pairs

def test_attention_window_narrows_with_fatigue():
    beliefs = {("o1", f"attr{i}", f"val{i}"): 0.9 for i in range(20)}
    win = AttentionWindow(beliefs)
    win.refresh(cycle=1)
    wide = len(win.pairs)
    win.refresh(cycle=10)
    narrow = len(win.pairs)
    assert narrow < wide
    assert narrow >= 3

def test_focus_steering():
    beliefs = {("apple", "color", "red"): 0.9, ("ball", "shape", "round"): 0.8}
    win = AttentionWindow(beliefs)
    win.focus("color")
    assert set(win.pairs) == {("color", "red")}

def test_focus_clears():
    beliefs = {("apple", "color", "red"): 0.9}
    win = AttentionWindow(beliefs)
    win.focus("color")
    win.focus(None)
    win.refresh()
    assert ("color", "red") in win.pairs

from organism import SelfQuestioner, Mind

def _make_questioner(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text(
        'rel 0.9::bel("apple", "color", "red")\n'
        'rel 0.8::bel("apple", "shape", "round")\n'
        'rel 0.7::bel("ball", "color", "red")\n'
    )
    from organism import BeliefStore
    store = BeliefStore(tmp_path)
    store.load()
    store.beliefs_map = {
        ("apple", "color", "red"): 0.9,
        ("apple", "shape", "round"): 0.8,
        ("ball", "color", "red"): 0.7,
    }
    mind = Mind(scl)
    mind.rebuild()
    return SelfQuestioner(store, mind, tmp_path)

def test_derives_new_belief(tmp_path):
    q = _make_questioner(tmp_path)
    # apple is red AND round -> new belief apple has "red+round" = true
    q.ask(("color", "red"), ("shape", "round"))
    assert q.store.conf(("apple", "red_round", "true")) is not None

def test_no_derivation_no_growth(tmp_path):
    q = _make_questioner(tmp_path)
    # nothing is red AND drinkable -> no new belief
    q.ask(("color", "red"), ("drinkable", "true"))
    assert len(q.store.beliefs()) == 3

def test_consolidation_strengthens(tmp_path):
    q = _make_questioner(tmp_path)
    q.ask(("color", "red"), ("shape", "round"))
    before = q.store.conf(("apple", "red_round", "true"))
    q.ask(("color", "red"), ("shape", "round"))
    after = q.store.conf(("apple", "red_round", "true"))
    assert after >= before

from organism import DreamEngine, Mind
import random

def _make_dreamer(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text(
        'rel 0.9::bel("apple", "color", "red")\n'
        'rel 0.8::bel("apple", "shape", "round")\n'
        'rel 0.7::bel("ball", "color", "red")\n'
        'rel 0.9::bel("ball", "shape", "round")\n'
    )
    from organism import BeliefStore
    store = BeliefStore(tmp_path)
    store.load()
    store.beliefs_map = {
        ("apple", "color", "red"): 0.9,
        ("apple", "shape", "round"): 0.8,
        ("ball", "color", "red"): 0.7,
        ("ball", "shape", "round"): 0.9,
    }
    mind = Mind(scl)
    mind.rebuild()
    return DreamEngine(store, mind)

def test_dream_generates_candidate_facts(tmp_path):
    engine = _make_dreamer(tmp_path)
    engine.rng = random.Random(42)
    dreams = engine.dream(count=3)
    assert len(dreams) == 3
    for d in dreams:
        assert isinstance(d, dict)
        assert "rule" in d and "combo" in d

def test_dream_validates_and_promotes(tmp_path):
    engine = _make_dreamer(tmp_path)
    engine.rng = random.Random(42)
    dreams = engine.dream(count=5)
    promoted = engine.validate(dreams)
    # at least one dream should promote (apple/ball share color+shape)
    assert len(promoted) >= 1

def test_dream_discards_unsupported(tmp_path):
    engine = _make_dreamer(tmp_path)
    unsupported = [{"rule": 'q99(x) = bel(x, "color", "red"), bel(x, "drinkable", "true")',
                    "combo": "red_true", "head": "q99"}]
    promoted = engine.validate(unsupported)
    assert promoted == []
