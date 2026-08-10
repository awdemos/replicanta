"""Mental-state feature: arousal/rationality/irrationality attributes,
the insane flag at extreme stress + incoherence, mood override, TUI/narration
exposure, and persistence."""

import pytest

from replicanta.organism import BeliefStore, MentalState, Organism


@pytest.fixture
def store(tmp_path):
    return BeliefStore(tmp_path)


def _mental(store):
    return MentalState(store)


def _drive(store, mental, chaos, ticks=40, sleeping=False):
    for _ in range(ticks):
        mental.tick(sleeping=sleeping, chaos=chaos, dt=1.0)


# -- attribute mechanics ----------------------------------------------------


def test_defaults(store):
    assert store.arousal == pytest.approx(0.3)
    assert store.rationality == pytest.approx(0.5)
    assert store.irrationality == pytest.approx(0.2)
    assert store.insane is False


def test_attributes_clamped(store):
    mental = _mental(store)
    store.stress = 1.0
    _drive(store, mental, chaos=1.0)
    for value in (store.arousal, store.rationality, store.irrationality):
        assert 0.0 <= value <= 1.0


def test_high_stress_and_chaos_raise_irrationality(store):
    mental = _mental(store)
    store.stress = 0.9
    _drive(store, mental, chaos=0.9)
    assert store.irrationality > 0.7
    assert store.rationality < 0.4


def test_sleep_lowers_arousal(store):
    mental = _mental(store)
    _drive(store, mental, chaos=0.9)  # get aroused first
    awake_arousal = store.arousal
    _drive(store, mental, chaos=0.1, sleeping=True)
    assert store.arousal < awake_arousal


def test_grounded_utterances_raise_rationality(store):
    mental = _mental(store)
    store.note_activity("llm_calls", 50)
    store.note_activity("grounded_utterances", 10)
    _drive(store, mental, chaos=0.2)
    assert store.rationality > 0.5


# -- insanity ----------------------------------------------------------------


def test_extreme_stress_with_incoherence_goes_insane(store):
    mental = _mental(store)
    store.stress = 0.9
    flipped = [mental.tick(sleeping=False, chaos=0.9, dt=1.0) for _ in range(40)]
    assert store.insane is True
    assert any(flipped)  # the flip was reported exactly at the transition


def test_calm_mind_stays_sane(store):
    mental = _mental(store)
    store.stress = 0.2
    _drive(store, mental, chaos=0.3)
    assert store.insane is False


def test_insanity_hysteresis(store):
    mental = _mental(store)
    store.stress = 0.9
    _drive(store, mental, chaos=0.9)
    assert store.insane is True
    # stress between SANE_STRESS and INSANE_STRESS: still insane
    store.stress = 0.7
    mental.tick(sleeping=False, chaos=0.9, dt=1.0)
    assert store.insane is True
    # stress below SANE_STRESS: recovers
    store.stress = 0.4
    mental.tick(sleeping=False, chaos=0.9, dt=1.0)
    assert store.insane is False


# -- organism integration -----------------------------------------------------


def test_insane_mood_wins(tmp_path):
    org = Organism(tmp_path)
    org.load()
    org.store.insane = True
    assert org._compute_mood() == "insane"


def test_tick_emits_mental_event_on_flip(tmp_path):
    org = Organism(tmp_path)
    org.load()
    org.store.stress = 0.95
    org.store.irrationality = 0.9
    events = org.tick(dt=1.0)
    assert {"kind": "mental", "insane": True} in events
    moods = [e["mood"] for e in events if e["kind"] == "mood"]
    assert "insane" in moods


def test_mental_attributes_persist(tmp_path):
    org = Organism(tmp_path)
    org.load()
    org.store.arousal = 0.77
    org.store.rationality = 0.11
    org.store.irrationality = 0.66
    org.store.insane = True
    org.flush(force=True)

    fresh = BeliefStore(tmp_path)
    fresh.load()
    assert fresh.arousal == pytest.approx(0.77)
    assert fresh.rationality == pytest.approx(0.11)
    assert fresh.irrationality == pytest.approx(0.66)
    assert fresh.insane is True


# -- narration ----------------------------------------------------------------


def test_mood_line_insane():
    from replicanta.narration import _mood_line

    assert "incoherent" in _mood_line("insane")


def test_snapshot_includes_mental_attributes(tmp_path):
    from replicanta.narration import state_snapshot

    org = Organism(tmp_path)
    org.load()
    snap = state_snapshot(org)
    for key in ("arousal", "rationality", "irrationality", "insane"):
        assert key in snap
