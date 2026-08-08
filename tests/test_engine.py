"""Engine feature: Organism.tick(dt) real-time loop — throttled sensing,
typed events at lifecycle transitions, debounced persistence (flush),
and the public force_state()/revive() commands the TUI drives."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from organism import BeliefStore, Lifecycle, Organism
from probe import SystemProbe


def _null_probe():
    """A probe over nonexistent trees: senses nothing, never distresses."""
    return SystemProbe(proc="/nonexistent/proc", sys="/nonexistent/sys")


def _organism(tmp_path, **kwargs):
    kwargs.setdefault("probe", _null_probe())
    org = Organism(tmp_path, **kwargs)
    org.load()
    return org


# -- tick events ------------------------------------------------------------

def test_tick_emits_sleep_and_dream_events_at_boundary(tmp_path):
    org = _organism(tmp_path, wake_seconds=0, sleep_seconds=999)
    events = org.tick(1.0)
    kinds = [e["kind"] for e in events]
    assert {"kind": "state", "to": "sleep"} in events
    assert "dream" in kinds
    assert org.lifecycle.state == "sleep"


def test_tick_idle_emits_no_state_events(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    events = org.tick(1.0)
    assert not [e for e in events if e["kind"] == "state"]
    assert org.lifecycle.state == "wake"


def test_tick_senses_on_first_call(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.tick(1.0)
    assert any((o, a) == ("time", "hour") for (o, a, _v) in org.store.beliefs())


def test_tick_fades_to_dead_event_under_critical_stress(tmp_path):
    org = _organism(tmp_path, wake_seconds=0, sleep_seconds=0)
    org.store.stress = 0.96
    for _ in range(Lifecycle.FADE_LIMIT - 1):
        org.tick(1.0)
    assert org.lifecycle.state != "dead"
    events = org.tick(1.0)
    assert org.lifecycle.state == "dead"
    assert {"kind": "state", "to": "dead"} in events


def test_tick_on_dead_is_quiet(tmp_path):
    org = _organism(tmp_path)
    org.lifecycle._transition("dead")
    assert org.tick(1.0) == []


def test_tick_emits_stress_event_crossing_band_upward(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.tick(1.0)  # baseline band
    org.store.stress = 0.6
    events = org.tick(1.0)
    assert {"kind": "stress", "band": 1} in events


def test_tick_no_stress_event_crossing_downward(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.store.stress = 0.6
    org.tick(1.0)
    org.store.stress = 0.1
    events = org.tick(1.0)
    assert not [e for e in events if e["kind"] == "stress"]


# -- debounced persistence ----------------------------------------------------

def test_flush_is_noop_when_clean(tmp_path):
    org = _organism(tmp_path)
    org.store.dirty = False
    assert org.flush() is False


def test_flush_persists_when_dirty_and_clears_flag(tmp_path):
    org = _organism(tmp_path)
    org.store.add(("cat", "color", "blue"), 0.8)
    assert org.store.dirty
    assert org.flush() is True
    assert not org.store.dirty
    assert (tmp_path / "state.json").exists()


def test_flush_rewrites_genome_only_on_belief_change(tmp_path):
    org = _organism(tmp_path)
    org.store.add(("cat", "color", "blue"), 0.8)
    org.flush()
    genome = (tmp_path / "organism.scl").read_text()
    # chat-only change: dirty but not genome_dirty -> no .scl rewrite
    org.store.record_chat("user", "hello")
    org.flush()
    assert (tmp_path / "organism.scl").read_text() == genome
    # belief change: genome rewritten
    org.store.add(("cat", "shape", "round"), 0.8)
    org.flush()
    assert (tmp_path / "organism.scl").read_text() != genome


def test_observe_unchanged_reading_stays_clean(tmp_path):
    org = _organism(tmp_path)
    org.store.add(("cpu", "load", "low"), 0.9)
    org.flush()
    org.store.observe(("cpu", "load", "low"), 0.9)
    assert not org.store.dirty


def test_commit_rule_marks_genome_dirty(tmp_path):
    store = BeliefStore(tmp_path)
    store.commit_rule('q1(x) = bel(x, "color", "blue")', 1)
    assert store.dirty and store.genome_dirty
    store.save()
    assert not store.dirty and not store.genome_dirty
    assert 'rel q1(x) = bel(x, "color", "blue")' in \
        (tmp_path / "organism.scl").read_text()


# -- front-end commands -------------------------------------------------------

def test_force_state_sleep_returns_events(tmp_path):
    org = _organism(tmp_path)
    events = org.force_state("sleep")
    assert {"kind": "state", "to": "sleep"} in events
    assert any(e["kind"] == "dream" for e in events)
    assert org.lifecycle.state == "sleep"


def test_force_state_same_state_is_noop(tmp_path):
    org = _organism(tmp_path)
    assert org.force_state("wake") == []


def test_force_state_rejects_unknown_target(tmp_path):
    org = _organism(tmp_path)
    with pytest.raises(ValueError):
        org.force_state("party")


def test_force_state_dead_is_noop(tmp_path):
    org = _organism(tmp_path)
    org.lifecycle._transition("dead")
    assert org.force_state("sleep") == []


def test_revive_only_when_dead(tmp_path):
    org = _organism(tmp_path)
    assert org.revive() is False
    org.store.stress = 0.96
    for _ in range(Lifecycle.FADE_LIMIT):
        org.lifecycle.tick()
    assert org.revive() is True
    assert org.lifecycle.state == "wake"
    assert org.store.stress == pytest.approx(0.05)
