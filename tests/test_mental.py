"""Mental-state feature: arousal/coherence/incoherence attributes,
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
    assert store.coherence == pytest.approx(0.5)
    assert store.incoherence == pytest.approx(0.2)
    assert store.insane is False


def test_attributes_clamped(store):
    mental = _mental(store)
    store.stress = 1.0
    _drive(store, mental, chaos=1.0)
    for value in (store.arousal, store.coherence, store.incoherence):
        assert 0.0 <= value <= 1.0


def test_high_stress_and_chaos_raise_incoherence(store):
    mental = _mental(store)
    store.stress = 0.9
    _drive(store, mental, chaos=0.9)
    assert store.incoherence > 0.7
    assert store.coherence < 0.4


def test_sleep_lowers_arousal(store):
    mental = _mental(store)
    _drive(store, mental, chaos=0.9)  # get aroused first
    awake_arousal = store.arousal
    _drive(store, mental, chaos=0.1, sleeping=True)
    assert store.arousal < awake_arousal


def test_grounded_utterances_raise_coherence(store):
    mental = _mental(store)
    store.note_activity("llm_calls", 50)
    store.note_activity("grounded_utterances", 10)
    _drive(store, mental, chaos=0.2)
    assert store.coherence > 0.5


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


def test_chaos_alone_cannot_unhinge(store):
    """Incoherence unravels from stress, not ambient chaos: even at maximum
    chaos a low-stress organism stays coherent (the 'too easy' fix)."""
    mental = _mental(store)
    store.stress = 0.2
    _drive(store, mental, chaos=1.0)
    assert store.incoherence < 0.45
    assert store.insane is False


def test_insanity_hysteresis(store):
    mental = _mental(store)
    store.stress = 0.9
    _drive(store, mental, chaos=0.9)
    assert store.insane is True
    # stress between RECOVERY_STRESS and INSANE_STRESS: still insane
    store.stress = 0.7
    mental.tick(sleeping=False, chaos=0.9, dt=1.0)
    assert store.insane is True
    # stress below RECOVERY_STRESS: recovers
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
    org.store.incoherence = 0.9
    events = org.tick(dt=1.0)
    assert {"kind": "mental", "insane": True} in events
    moods = [e["mood"] for e in events if e["kind"] == "mood"]
    assert "insane" in moods


def test_mental_attributes_persist(tmp_path):
    org = Organism(tmp_path)
    org.load()
    org.store.arousal = 0.77
    org.store.coherence = 0.11
    org.store.incoherence = 0.66
    org.store.insane = True
    org.flush(force=True)

    fresh = BeliefStore(tmp_path)
    fresh.load()
    assert fresh.arousal == pytest.approx(0.77)
    assert fresh.coherence == pytest.approx(0.11)
    assert fresh.incoherence == pytest.approx(0.66)
    assert fresh.insane is True


# -- narration ----------------------------------------------------------------


def test_mood_line_insane():
    from replicanta.narration import _mood_line

    assert "incoherent" in _mood_line("insane")


def test_mood_line_descent_stages():
    from replicanta.narration import _mood_line

    assert "slipping" in _mood_line("fraying")
    assert "losing its grip" in _mood_line("unhinged")


# -- staged descent moods ------------------------------------------------------


def _mood_org(tmp_path):
    org = Organism(tmp_path)
    org.load()
    return org


def test_fraying_and_unhinged_moods_stage_the_descent(tmp_path):
    org = _mood_org(tmp_path)
    org.store.stress = 0.3  # below the anxious threshold: mood tracks incoherence
    org.store.incoherence = 0.55
    assert org._compute_mood() == "fraying"
    org.store.incoherence = 0.65
    assert org._compute_mood() == "unhinged"
    org.store.insane = True
    assert org._compute_mood() == "insane"  # the flag still wins


def test_descent_stage_hysteresis(tmp_path):
    org = _mood_org(tmp_path)
    org.store.stress = 0.3
    org.store.incoherence = 0.55
    assert org._update_mood() == "fraying"
    org.store.incoherence = 0.47  # dips inside the band: mood holds
    assert org._update_mood() is None  # unchanged: still fraying
    org.store.incoherence = 0.42  # out of the band: falls back through
    assert org._update_mood() == "calm"


def test_anxious_still_precedes_the_descent(tmp_path):
    org = _mood_org(tmp_path)
    org.store.stress = 0.55
    org.store.incoherence = 0.2
    assert org._compute_mood() == "anxious"


def test_snapshot_includes_mental_attributes(tmp_path):
    from replicanta.narration import state_snapshot

    org = Organism(tmp_path)
    org.load()
    snap = state_snapshot(org)
    for key in ("arousal", "coherence", "incoherence", "insane"):
        assert key in snap
