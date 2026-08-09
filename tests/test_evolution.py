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
