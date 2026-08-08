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

def test_scl_with_committed_rules_reimports(tmp_path):
    store = BeliefStore(tmp_path)
    store.add(("apple", "color", "red"), 0.9)
    store.add(("apple", "shape", "round"), 0.8)
    store.rules.append(('q1(x) = bel(x, "color", "red"), bel(x, "shape", "round")', 1))
    store.save()
    mind = Mind(tmp_path / "organism.scl")
    mind.rebuild()
    assert mind.beliefs()[("apple", "color", "red")] == 0.9

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

def test_chaos_generalization_commits_rule_with_depth(monkeypatch, tmp_path):
    q = _make_questioner(tmp_path)
    q.store.chaos = 1.0
    q.store.rules.append(('q0(x) = bel(x, "color", "red")', 1))
    monkeypatch.setattr(random, "random", lambda: 0.0)
    q.ask(("color", "red"), ("shape", "round"))
    rule, depth = q.store.rules[-1]
    assert depth == 2
    assert rule.startswith("q1(x)")

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

from organism import Lifecycle, Metrics, BeliefStore

def test_lifecycle_advances_cycle(monkeypatch, tmp_path):
    store = BeliefStore(tmp_path)
    lc = Lifecycle(store, wake_seconds=0, sleep_seconds=0)
    lc.tick()  # forces wake -> sleep transition
    assert store.cycle == 1
    assert lc.state in ("sleep", "wake")

def test_metrics_score_components(tmp_path):
    store = BeliefStore(tmp_path)
    store.beliefs_map = {("a", "color", "red"): 0.9, ("b", "shape", "round"): 0.8}
    store.rules = [('q1(x) = bel(x, "color", "red")', 1)]
    m = Metrics(store)
    assert m.belief_count == 2
    assert m.rule_count == 1
    assert m.score() > 0

def test_metrics_score_monotonic_under_prune_archive(tmp_path):
    store = BeliefStore(tmp_path)
    store.add(("apple", "color", "red"), 0.9)
    store.add(("apple", "color", "green"), 0.6)
    m1 = Metrics(store).score()
    store.add(("ball", "shape", "round"), 0.8)
    m2 = Metrics(store).score()
    assert m2 >= m1

from organism import Organism

def _seeded_organism(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text(
        'rel 0.9::bel("apple", "color", "red")\n'
        'rel 0.8::bel("apple", "shape", "round")\n'
        'rel 0.7::bel("ball", "color", "red")\n'
        'rel 0.9::bel("ball", "shape", "round")\n'
    )
    return Organism(tmp_path, wake_seconds=0, sleep_seconds=0)

def test_organism_bootstraps_from_genome(tmp_path):
    org = _seeded_organism(tmp_path)
    org.load()
    assert org.store.conf(("apple", "color", "red")) == 0.9
    assert org.store.conf(("ball", "shape", "round")) == 0.9
    assert org.metrics().score() > 0

def test_organism_sleeps_and_grows(tmp_path):
    org = _seeded_organism(tmp_path)
    org.load()
    score_before = org.metrics().score()
    org.cycle()
    score_after = org.metrics().score()
    assert score_after >= score_before
    assert org.store.cycle >= 1

def test_organism_self_play_grows_over_cycles(tmp_path):
    org = _seeded_organism(tmp_path)
    org.load()
    scores = [org.metrics().score()]
    for _ in range(5):
        org.cycle()
        scores.append(org.metrics().score())
    assert scores[-1] >= scores[0]


from tui import OrganismApp

def test_tui_app_constructs(tmp_path):
    from organism import Organism
    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    assert app is not None

def test_tui_command_chaos(tmp_path):
    from organism import Organism
    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    app.handle_command("/chaos 0.8")
    assert org.store.chaos == 0.8

def test_tui_command_focus(tmp_path):
    from organism import Organism
    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    app.handle_command("/focus color")
    assert org.window.focus_attr == "color"
