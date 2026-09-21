"""Goals feature: the organism forms intentions and pursues them across
sessions — store persistence, tick events (want_goal / goal completion),
narration.form_goal, and goal injection into prompts."""

import json

from conftest import patch_generate

from replicanta import goals, llmclient, narration, voice
from replicanta.organism import BeliefStore, Organism
from replicanta.probe import SystemProbe


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


def test_store_refuses_second_active_goal(tmp_path):
    # one active goal at a time: a new goal is refused (returns None,
    # nothing appended) while another is still active, and done-goal
    # history stays intact
    store = BeliefStore(tmp_path)
    first = store.add_goal("learn about the user", marker=0)
    assert first is not None
    assert store.add_goal("answer: what is rain", marker=0) is None
    assert len(store.goals) == 1
    assert store.active_goal()["text"] == "learn about the user"
    store.complete_active_goal()
    third = store.add_goal("answer: what is rain", marker=0)
    assert third is not None
    assert store.active_goal()["text"] == "answer: what is rain"
    assert sum(1 for g in store.goals if g["done_cycle"] is not None) == 1


def test_load_drops_partial_goal_dicts(tmp_path):
    """Goal entries missing keys that active_goal()/_goals_tick/mind_view
    index (text, created_cycle, done_cycle, marker) are dropped on load;
    active_goal() must never KeyError on a corrupt state.json."""
    store = BeliefStore(tmp_path)
    store.state_path.parent.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text(
        json.dumps(
            {
                "goals": [
                    {"text": "ok", "created_cycle": 1, "done_cycle": None, "marker": 2},
                    {"created_cycle": 1, "done_cycle": None, "marker": 2},  # no text
                    {"text": "no done_cycle key", "created_cycle": 1, "marker": 2},
                    {"text": "bad done stamp", "created_cycle": 1, "done_cycle": "later", "marker": 0},
                    {"text": "no marker", "created_cycle": 1, "done_cycle": None},
                    "not a goal",
                ]
            }
        )
    )
    store.load()
    assert [g["text"] for g in store.goals] == ["ok"]
    assert store.active_goal()["text"] == "ok"


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


def test_generic_goal_reports_stalled_after_no_progress(tmp_path):
    # _goals_tick and goals.goal_progress track the same series (the
    # user-fact count), so after STALLED_CYCLES ticks without movement the
    # stall marker actually shows
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.add_goal("understand what a week is")
    org.store.cycle = 10
    org.tick(1.0)  # stamps last progress at cycle 10
    assert "(stalled)" not in goals.goal_progress(org.store)
    org.store.cycle = 10 + goals.STALLED_CYCLES + 1
    events = org.tick(1.0)
    assert any(e["kind"] == "goal_stalled" for e in events)
    line = goals.goal_progress(org.store)
    assert line is not None and "(stalled)" in line


def test_learn_goal_progress_matches_tick_metric(tmp_path):
    # the progress figure in the goal line is the same one _goals_tick
    # records, so new user facts move it and reset the stall clock
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.add_goal("learn about the user")
    org.store.cycle = 10
    org.tick(1.0)
    org.store.cycle = 10 + goals.STALLED_CYCLES + 1
    org.hear("my name is sam")  # a new user fact: progress moves
    org.tick(1.0)
    assert not [e for e in org.tick(1.0) if e["kind"] == "goal_stalled"]


def test_organism_add_goal_refuses_while_active(tmp_path):
    org = _organism(tmp_path)
    assert org.add_goal("learn about the user") is not None
    assert org.add_goal("climb a mountain") is None
    assert org.store.active_goal()["text"] == "learn about the user"
    assert not any("climb a mountain" in m["text"] for m in org.store.memory)


def test_hear_question_does_not_stack_second_active_goal(tmp_path):
    org = _organism(tmp_path)
    org.add_goal("learn about the user")
    org.hear("what is a scallop?")  # would enqueue an "answer:" goal
    active = [g for g in org.store.goals if g["done_cycle"] is None]
    assert len(active) == 1
    assert active[0]["text"] == "learn about the user"


def test_add_goal_remembers_episode(tmp_path):
    org = _organism(tmp_path)
    org.add_goal("learn about the user")
    assert any(m["kind"] == "goal" and "learn" in m["text"] for m in org.store.memory)


# -- narration ----------------------------------------------------------------


def test_form_goal_prompt_branch(tmp_path, monkeypatch):
    org = _organism(tmp_path)
    captured = {}
    patch_generate(monkeypatch, lambda prompt, *a, **k: captured.setdefault("p", prompt) or "x")
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
