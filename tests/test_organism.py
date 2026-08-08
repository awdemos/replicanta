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
