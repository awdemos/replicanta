"""Stress feature: StressMeter mechanics, adverse hooks, chaos coupling,
harshness scorer, and narration/TUI exposure."""

import pytest

from replicanta.organism import (
    BeliefStore,
    DreamEngine,
    Lifecycle,
    Mind,
    Organism,
    SelfQuestioner,
    StressMeter,
)
from replicanta.sentiment import harshness


@pytest.fixture
def store(tmp_path):
    return BeliefStore(tmp_path)


def _meter(store):
    return StressMeter(store)


# -- StressMeter mechanics -------------------------------------------------


def test_baseline_stress(store):
    meter = _meter(store)
    assert meter.value == pytest.approx(0.05)


def test_bump_raises_stress(store):
    meter = _meter(store)
    meter.bump(0.1)
    assert meter.value == pytest.approx(0.15)


def test_bump_clamps_at_one(store):
    meter = _meter(store)
    meter.bump(2.0)
    assert meter.value == pytest.approx(1.0)


def test_sleep_recovers_toward_baseline(store):
    meter = _meter(store)
    store.stress = 0.6
    meter.tick(sleeping=True, dt=10.0)
    assert meter.value < 0.6
    assert meter.value >= 0.05  # never below baseline


def test_wake_accumulates_sleep_debt(store):
    meter = _meter(store)
    store.stress = 0.05  # at baseline
    meter.tick(sleeping=False, dt=10.0)
    assert meter.value > 0.05  # upward pressure while awake


def test_sleep_recovery_faster_than_wake_decay(store):
    meter = _meter(store)
    store.stress = 0.8
    meter.tick(sleeping=True, dt=1.0)
    slept = meter.value
    store.stress = 0.8
    meter.tick(sleeping=False, dt=1.0)
    woke = meter.value
    assert (0.8 - slept) > (0.8 - woke)


def test_negative_mood_pressure(store):
    meter = _meter(store)
    store.add(("self", "mood", "sad"), 0.9)
    store.stress = 0.3
    meter.tick(sleeping=False, dt=10.0)
    stressed = meter.value
    store.beliefs_map.pop(("self", "mood", "sad"))
    store.stress = 0.3
    meter.tick(sleeping=False, dt=10.0)
    neutral = meter.value
    assert stressed > neutral


def test_calm_mood_no_pressure(store):
    meter = _meter(store)
    store.add(("self", "mood", "calm"), 0.9)
    store.stress = 0.3
    meter.tick(sleeping=False, dt=10.0)
    assert meter.value <= 0.3 + 10.0 * StressMeter.SLEEP_DEBT_RATE + 1e-9


def test_bump_lingers_tens_of_minutes(store):
    """Adversity stays emotionally readable: a 0.15 bump is still clearly
    present after 10 minutes awake (regression: constant decay erased
    bumps within a couple of minutes, so the gauge never visibly moved)."""
    meter = _meter(store)
    meter.bump(0.15)
    for _ in range(600):
        meter.tick(sleeping=False, dt=1.0)
    assert meter.value > 0.15


def test_wake_pressure_is_bounded(store):
    """A long calm day is taxing but can never approach the fade zone on
    wake pressure alone (regression: decay >= pressure pinned the gauge
    at 0.05 forever; mood pressure then ratcheted it to 1.0)."""
    meter = _meter(store)
    for _ in range(120):  # 2h awake
        meter.tick(sleeping=False, dt=60.0)
    assert StressMeter.BASELINE < meter.value < 0.6


def test_negative_mood_pressure_is_bounded(store):
    """A sustained draining mood approaches but stays below the fade zone
    without fresh adverse events."""
    meter = _meter(store)
    store.add(("self", "mood", "sad"), 0.9)
    for _ in range(120):  # 2h awake in a draining mood
        meter.tick(sleeping=False, dt=60.0)
    assert meter.value < 0.8
    assert meter.value < Lifecycle.FADE_STRESS


def test_sleep_clears_bumps_faster_than_wake(store):
    """Sleep bleeds an upset off in minutes; the same bump lingers (even
    grows slightly under wake pressure) through a wake period."""
    meter = _meter(store)
    meter.bump(0.3)
    for _ in range(300):
        meter.tick(sleeping=True, dt=1.0)
    assert meter.value < 0.25
    store.stress = 0.35
    for _ in range(300):
        meter.tick(sleeping=False, dt=1.0)
    assert meter.value > 0.30


# -- soothe ------------------------------------------------------------------


def _org(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text('rel 0.9::bel("apple", "color", "red")\n')
    org = Organism(tmp_path)
    org.load()
    return org


def test_soothe_relieves_half_the_excess(tmp_path):
    org = _org(tmp_path)
    org.store.stress = 0.8
    relief = org.soothe()
    assert relief == pytest.approx(0.375)
    assert org.store.stress == pytest.approx(0.425)


def test_soothe_floors_at_baseline(tmp_path):
    org = _org(tmp_path)
    org.store.stress = 0.12
    relief = org.soothe()
    assert org.store.stress == pytest.approx(StressMeter.BASELINE)
    assert relief == pytest.approx(0.12 - StressMeter.BASELINE)


def test_soothe_at_ease_is_noop(tmp_path):
    org = _org(tmp_path)
    org.store.stress = StressMeter.BASELINE
    assert org.soothe() == 0.0
    assert org.store.stress == pytest.approx(StressMeter.BASELINE)


def test_soothe_reads_as_kindness(tmp_path):
    org = _org(tmp_path)
    org.store.stress = 0.8
    org.soothe()
    assert org._sentiment is not None and org._sentiment[0] == "kind"
    assert any("soothed" in m.get("text", "") for m in org.store.memory)


# -- persistence -----------------------------------------------------------


def test_stress_persists_in_state_json(tmp_path):
    store = BeliefStore(tmp_path)
    store.stress = 0.42
    store.save()
    loaded = BeliefStore(tmp_path)
    loaded.load()
    assert loaded.stress == pytest.approx(0.42)


def test_stress_defaults_to_baseline_on_load(tmp_path):
    store = BeliefStore(tmp_path)
    store.save()
    loaded = BeliefStore(tmp_path)
    loaded.load()
    assert loaded.stress == pytest.approx(0.05)


# -- adverse hooks ---------------------------------------------------------


def test_contradiction_bumps_stress(store):
    meter = _meter(store)
    store.on_adverse = meter.bump
    store.add(("apple", "color", "red"), 0.9)
    before = meter.value
    store.add(("apple", "color", "green"), 0.6)
    assert meter.value > before
    assert meter.value == pytest.approx(before + 0.03)


def test_failed_question_bumps_stress(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text('rel 0.9::bel("apple", "color", "red")\n')
    store = BeliefStore(tmp_path)
    store.load()
    store.beliefs_map = {("apple", "color", "red"): 0.9}
    mind = Mind(scl)
    mind.rebuild()
    meter = _meter(store)
    q = SelfQuestioner(store, mind, tmp_path, stress=meter)
    before = meter.value
    q.ask(("color", "red"), ("drinkable", "true"))
    assert meter.value == pytest.approx(before + 0.01)


def test_discarded_dream_bumps_stress(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text('rel 0.9::bel("apple", "color", "red")\n')
    store = BeliefStore(tmp_path)
    store.load()
    store.beliefs_map = {("apple", "color", "red"): 0.9}
    mind = Mind(scl)
    mind.rebuild()
    meter = _meter(store)
    engine = DreamEngine(store, mind, stress=meter)
    before = meter.value
    unsupported = [
        {
            "rule": 'q99(x) = bel(x, "color", "red"), bel(x, "drinkable", "true")',
            "combo": "red_true",
            "head": "q99",
        }
    ]
    engine.promote(unsupported)
    assert meter.value == pytest.approx(before + 0.04)


def test_promoted_dream_no_stress(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text(
        'rel 0.9::bel("apple", "color", "red")\n'
        'rel 0.8::bel("apple", "shape", "round")\n'
        'rel 0.7::bel("ball", "color", "red")\n'
        'rel 0.9::bel("ball", "shape", "round")\n'
    )
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
    meter = _meter(store)
    engine = DreamEngine(store, mind, stress=meter)
    engine.rng = __import__("random").Random(42)
    before = meter.value
    dreams = engine.dream(count=5)
    engine.promote(dreams)
    assert meter.value == pytest.approx(before)


# -- chaos coupling --------------------------------------------------------


def test_chaos_effective_boosted_by_stress(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text('rel 0.9::bel("apple", "color", "red")\n')
    org = Organism(tmp_path)
    org.load()
    org.store.chaos = 0.5
    org.store.stress = 0.8
    assert org.chaos_effective() == pytest.approx(min(1.0, 0.5 + 0.3 * 0.3))
    org.store.stress = 0.4
    assert org.chaos_effective() == pytest.approx(0.5)


def test_chaos_effective_clamps_at_one(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text('rel 0.9::bel("apple", "color", "red")\n')
    org = Organism(tmp_path)
    org.load()
    org.store.chaos = 0.9
    org.store.stress = 1.0
    assert org.chaos_effective() == pytest.approx(1.0)


def test_wake_uses_effective_chaos_for_question_count(tmp_path, monkeypatch):
    scl = tmp_path / "organism.scl"
    scl.write_text(
        'rel 0.9::bel("apple", "color", "red")\n'
        'rel 0.8::bel("apple", "shape", "round")\n'
        'rel 0.7::bel("ball", "color", "red")\n'
    )
    org = Organism(tmp_path)
    org.load()
    # stress high -> chaos_effective() > 0.5 -> 3 questions instead of 2
    org.store.stress = 1.0
    org.store.chaos = 0.5
    asked = []

    from replicanta import organism as organism_module

    def spy_derive(genome_text, rule, head):
        asked.append(1)
        return []

    monkeypatch.setattr(organism_module, "derive_in_thread", spy_derive)
    org._wake()
    assert len(asked) == 3


# -- harshness -------------------------------------------------------------


def test_harshness_zero_for_neutral():
    assert harshness("hello there, how are you?") == 0.0


def test_harshness_detects_insults():
    assert harshness("you are useless and stupid") > 0.0


def test_harshness_caps_at_limit():
    very_harsh = "stupid useless pathetic idiot worthless dumb"
    assert harshness(very_harsh) <= 0.15


def test_harshness_case_insensitive():
    assert harshness("SHUT UP") > 0.0
