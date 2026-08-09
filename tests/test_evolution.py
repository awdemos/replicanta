"""Regression tests for organism evolution, goal-seeking, and self-awareness
enhancements: activity digest, goal progress, reflection triggers, etc."""

import sys
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import activity
from narration import build_prompt, state_snapshot
from organism import BeliefStore, Lifecycle, Metrics


class FakeWindow:
    pairs: ClassVar[set] = {("has_fur", "true")}


class FakeOrg:
    """Minimal organism stand-in for evolution tests."""

    def __init__(self, tmp_path):
        self.store = BeliefStore(tmp_path)
        self.store.cycle = 10
        self.store.chaos = 0.5
        self.store.add(("cat", "has_fur", "true"), 0.9)
        self.store.add(("cat", "has_paws", "true"), 0.8)
        self.lifecycle = Lifecycle(self.store)
        self.window = FakeWindow()
        self.skills = None

    def metrics(self):
        return Metrics(self.store)


@pytest.fixture
def org(tmp_path):
    return FakeOrg(tmp_path)


def _add_activity(store):
    """Populate store.activity with realistic counters."""
    counters = {
        "rules_tried": 12,
        "derivations": 3,
        "beliefs_new": 2,
        "beliefs_strengthened": 1,
        "beliefs_archived": 0,
        "rules_committed": 1,
        "dreams_promoted": 1,
        "dreams_discarded": 5,
        "llm_calls": 8,
        "prompt_tokens": 1200,
        "gen_tokens": 400,
        "utterances": 6,
        "fallbacks": 1,
        "facts_learned": 1,
        "grounded_utterances": 4,
    }
    if not store.activity:
        store.activity = counters
    else:
        store.activity.update(counters)


def test_activity_digest_reports_counters(org):
    _add_activity(org.store)
    text = activity.digest(org.store)
    assert "self-questions" in text
    assert "derivations" in text
    assert "rules" in text
    assert "dreams" in text


def test_activity_digest_snapshot_pruning(org):
    _add_activity(org.store)
    org.store.cycle = 5
    activity.digest(org.store, cycles=30)
    org.store.cycle = 50
    _add_activity(org.store)
    text = activity.digest(org.store, cycles=30)
    # Should compare against a snapshot within the last 30 cycles.
    assert "last 30 cycles" in text or "last 45 cycles" in text


def test_state_snapshot_includes_activity_digest(org):
    _add_activity(org.store)
    snap = state_snapshot(org)
    assert "activity_digest" in snap
    assert "derivations" in snap["activity_digest"]


def test_build_prompt_includes_activity_digest(org):
    _add_activity(org.store)
    prompt = build_prompt(state_snapshot(org))
    assert "recent learning activity" in prompt
    assert "self-questions" in prompt


def test_goal_progress_in_prompt(org):
    org.store.add_goal("learn three things about the user", marker=0)
    org.store.add(("user", "name", "ada"), 0.8)
    snap = state_snapshot(org)
    assert snap.get("goal_progress")
    assert "progress" in snap["goal_progress"]
    prompt = build_prompt(snap)
    assert "goal:" in prompt


def test_goal_strategy_renders_in_prompt(org):
    org.store.add_goal("learn about the user", marker=0,
                       strategy="strategy: ask one question at a time.")
    prompt = build_prompt(state_snapshot(org))
    assert "strategy:" in prompt


def test_surprise_recorded_on_contradiction(org):
    org.store.add(("user", "feeling", "happy"), 0.8)
    org.store.add(("user", "feeling", "sad"), 0.9)
    surprises = org.store.activity.get("surprises", [])
    assert len(surprises) >= 1
    assert "happy" in surprises[-1]["old"] or "sad" in surprises[-1]["old"]


def test_surprises_appear_in_prompt(org):
    org.store.add(("user", "feeling", "happy"), 0.8)
    org.store.add(("user", "feeling", "sad"), 0.9)
    prompt = build_prompt(state_snapshot(org))
    assert "recent surprises" in prompt
    assert "happy" in prompt or "sad" in prompt


def test_self_model_belief_renders(org):
    org.store.add(("self", "insight", "ask_about_user"), 0.7)
    snap = state_snapshot(org)
    assert snap.get("self_model")
    assert any("insight" in m for m in snap["self_model"])
    prompt = build_prompt(snap)
    assert "what you know about yourself" in prompt


def test_attention_rationale_renders(org):
    org.window.rationale = "you are focused on fur because it keeps coming up"
    snap = state_snapshot(org)
    assert snap.get("attention_rationale")
    prompt = build_prompt(snap)
    assert "where your attention is" in prompt


def test_skill_effectiveness_appears_in_prompt(org):
    from skills import Skill, SkillStore
    skills_dir = org.store.dir_path / "skills"
    store = SkillStore(skills_dir)
    store.save(Skill(name="rain talk", when="user likes rain",
                     how="ask a follow-up", uses=3, effectiveness=0.6))
    org.skills = store
    org.store.add(("user", "like_rain", "true"), 0.8)
    snap = state_snapshot(org)
    assert snap["skills"]
    assert "effectiveness" in snap["skills"][0]
