"""Goals feature: the organism forms intentions and pursues them across
sessions — store persistence, tick events (want_goal / goal completion),
narration.form_goal, and goal injection into prompts."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import narration
import llmclient
import voice
from organism import BeliefStore, Organism
from probe import SystemProbe


def _null_probe():
    return SystemProbe(proc="/nonexistent/proc", sys="/nonexistent/sys")


def _organism(tmp_path, **kwargs):
    kwargs.setdefault("probe", _null_probe())
    org = Organism(tmp_path, **kwargs)
    org.load()
    return org


# -- store -------------------------------------------------------------------

def test_store_goal_add_active_complete(tmp_path):
    store = BeliefStore(tmp_path)
    assert store.active_goal() is None
    store.add_goal("learn five things about the user", marker=1)
    goal = store.active_goal()
    assert goal["text"] == "learn five things about the user"
    assert goal["marker"] == 1
    store.complete_active_goal()
    assert store.active_goal() is None
    assert store.goals[0]["done_cycle"] is not None


def test_store_goals_persist_round_trip(tmp_path):
    store = BeliefStore(tmp_path)
    store.add_goal("understand rain", marker=0)
    store.save()
    store2 = BeliefStore(tmp_path)
    store2.load()
    assert store2.active_goal()["text"] == "understand rain"
    assert "last_goal_cycle" in store2.__dict__


# -- engine events -----------------------------------------------------------

def test_tick_emits_want_goal_once(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.store.cycle = 25
    kinds = [e["kind"] for e in org.tick(1.0)]
    assert "want_goal" in kinds
    # stamped immediately: no repeat while the voice is still working
    kinds = [e["kind"] for e in org.tick(1.0)]
    assert "want_goal" not in kinds


def test_tick_no_want_goal_with_active_goal(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.add_goal("learn about the user")
    org.store.cycle = 50
    kinds = [e["kind"] for e in org.tick(1.0)]
    assert "want_goal" not in kinds


def test_learn_goal_completes_after_two_new_user_facts(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.add_goal("learn about the user")
    org.store.cycle = 5
    org.store.add(("user", "name", "sam"), 0.8)
    org.store.add(("user", "like_rain", "true"), 0.8)
    events = org.tick(1.0)
    goal_events = [e for e in events if e["kind"] == "goal"]
    assert goal_events and goal_events[0]["done"] is True
    assert org.store.active_goal() is None
    assert any(m["kind"] == "goal" for m in org.store.memory)


def test_generic_goal_completes_after_pursuit_cycles(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.add_goal("understand what a week is")
    org.store.cycle = 5
    assert not [e for e in org.tick(1.0) if e["kind"] == "goal"]
    org.store.cycle = 5 + Organism.GOAL_PURSUIT_CYCLES
    events = org.tick(1.0)
    assert any(e["kind"] == "goal" and e["done"] for e in events)


def test_add_goal_remembers_episode(tmp_path):
    org = _organism(tmp_path)
    org.add_goal("learn about the user")
    assert any(m["kind"] == "goal" and "learn" in m["text"]
               for m in org.store.memory)


# -- narration ----------------------------------------------------------------

def test_form_goal_prompt_branch(tmp_path, monkeypatch):
    org = _organism(tmp_path)
    captured = {}
    monkeypatch.setattr(
        "llmclient.generate",
        lambda prompt, *a, **k: captured.setdefault("p", prompt) or "x")
    voice.form_goal(org)
    assert "one thing you want" in captured["p"]


def test_form_goal_fallback_deterministic(tmp_path):
    org = _organism(tmp_path)
    llmclient._voice.online = False
    goal = voice.form_goal(org)
    assert goal
    assert len(goal.split()) >= 3


def test_active_goal_appears_in_prompt(tmp_path):
    org = _organism(tmp_path)
    org.add_goal("learn about the user")
    prompt = narration.build_prompt(narration.state_snapshot(org))
    assert "what you are trying to do" in prompt
    assert "learn about the user" in prompt
