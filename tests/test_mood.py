"""Mood feature: sentiment scorers (harshness/kindness), Organism.hear()
tone coupling (bruise/soothe), the mood belief dynamics, and narration's
felt mood line."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from narration import _felt_experience, state_snapshot
from organism import Organism, StressMeter
from probe import SystemProbe
from sentiment import harshness, kindness


def _organism(tmp_path, **kwargs):
    kwargs.setdefault("probe", SystemProbe(proc="/nonexistent/proc",
                                           sys="/nonexistent/sys"))
    org = Organism(tmp_path, **kwargs)
    org.load()
    return org


def _mood_belief(org):
    return next((v for (o, a, v) in org.store.beliefs()
                 if (o, a) == ("self", "mood")), None)


# -- kindness scorer ---------------------------------------------------------

def test_kindness_zero_for_neutral():
    assert kindness("the weather is a topic") == 0.0


def test_kindness_detects_warmth():
    assert kindness("thank you, you are a good friend") > 0.0


def test_kindness_caps_at_limit():
    assert kindness("good good good love love please thanks great nice") \
        <= 0.02


def test_harshness_still_scored():
    assert harshness("you are useless") > 0.0


# -- hear(): tone touches the body --------------------------------------------

def test_hear_records_chat(tmp_path):
    org = _organism(tmp_path)
    org.hear("hello little one")
    assert org.store.chat_log[-1] == ["user", "hello little one"]


def test_hear_harsh_bruises(tmp_path):
    org = _organism(tmp_path)
    before = org.store.stress
    org.hear("you are useless and stupid")
    assert org.store.stress > before


def test_hear_kind_soothes(tmp_path):
    org = _organism(tmp_path)
    org.store.stress = 0.4
    org.hear("thank you, good little friend")
    assert org.store.stress < 0.4


def test_hear_kind_never_below_baseline(tmp_path):
    org = _organism(tmp_path)
    org.hear("thank you good friend")
    assert org.store.stress >= StressMeter.BASELINE


# -- mood dynamics -------------------------------------------------------------

def test_first_tick_settles_calm(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    events = org.tick(1.0)
    assert {"kind": "mood", "mood": "calm"} in events
    assert _mood_belief(org) == "calm"


def test_harsh_words_make_it_hurt(tmp_path):
    org = _organism(tmp_path)
    events = org.hear("shut up, you are pathetic")
    assert {"kind": "mood", "mood": "hurt"} in events
    assert _mood_belief(org) == "hurt"


def test_kind_words_make_it_grateful(tmp_path):
    org = _organism(tmp_path)
    events = org.hear("thank you, good friend")
    assert {"kind": "mood", "mood": "grateful"} in events


def test_strained_body_is_anxious(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.store.stress = 0.6
    events = org.tick(1.0)
    assert {"kind": "mood", "mood": "anxious"} in events


def test_hurt_beats_anxious(tmp_path):
    org = _organism(tmp_path)
    org.store.stress = 0.6
    events = org.hear("you are trash")
    assert {"kind": "mood", "mood": "hurt"} in events


def test_sentiment_expires_back_to_calm(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.tick(1.0)
    org.hear("you are trash")
    assert _mood_belief(org) == "hurt"
    org._sentiment = ("harsh", time.time() - 999)
    events = org.tick(1.0)
    assert {"kind": "mood", "mood": "calm"} in events


def test_mood_belief_confidence(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.tick(1.0)
    assert org.store.conf(("self", "mood", "calm")) == pytest.approx(0.9)


def test_revive_clears_mood_state(tmp_path):
    org = _organism(tmp_path)
    org.hear("you are garbage")
    assert _mood_belief(org) == "hurt"
    org.lifecycle._transition("dead")
    org.revive()
    events = org.tick(1.0)
    assert {"kind": "mood", "mood": "calm"} in events


# -- narration exposure ---------------------------------------------------------

def test_snapshot_includes_mood(tmp_path):
    org = _organism(tmp_path)
    org.hear("you are trash")
    assert state_snapshot(org)["mood"] == "hurt"


def test_felt_experience_has_mood_line():
    snap = {"chaos": 0.5, "stress": 0.1, "score": 1.0, "belief_count": 2,
            "mood": "grateful"}
    assert any("grateful" in line for line in _felt_experience(snap))
