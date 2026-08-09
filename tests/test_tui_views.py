import sys
from pathlib import Path

import pytest
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))

import tui_views
from organism import BeliefStore, Metrics


class FakeOrg:
    """Minimal organism stand-in: pure-Python store, enough for the views."""

    def __init__(self, tmp_path):
        self.store = BeliefStore(tmp_path)
        self.store.cycle = 3

    def metrics(self):
        return Metrics(self.store)


@pytest.fixture
def org(tmp_path):
    return FakeOrg(tmp_path)


def test_conf_bar():
    assert tui_views.conf_bar(1.0) == "▮▮▮▮▮"
    assert tui_views.conf_bar(0.0) == "▯▯▯▯▯"
    assert tui_views.conf_bar(0.5) in ("▮▮▮▯▯", "▮▮▯▯▯")


def test_mind_view_shows_top_beliefs(org):
    org.store.add(("self", "likes", "rain"), 0.9)
    org.store.add(("self", "fears", "fading"), 0.4)
    view = tui_views.mind_view(org)
    assert "top beliefs" in view
    assert "self:likes=rain" in view
    assert "genome:" in view


def test_mind_view_shows_rules_and_attention(org):
    org.store.commit_rule('q1(x) = bel(x, "a", "b")', 1)
    org.store.attention = {("color", "blue")}
    view = tui_views.mind_view(org)
    assert "committed rules" in view
    assert "attention:" in view
    assert "color=blue" in view


def test_memory_view_shows_episodes(org):
    org.store.remember("learned", "your name is sam")
    view = tui_views.memory_view(org)
    assert "episodes" in view
    assert "learned" in view
    assert "your name is sam" in view


def test_memory_view_shows_user_facts_and_self_view(org):
    org.store.add(("user", "name", "sam"), 0.8)
    org.store.add(("self", "described_as", "brave"), 0.8)
    view = tui_views.memory_view(org)
    assert "what it knows about you" in view
    assert "your name is sam" in view
    assert "what you said it is" in view
    assert "brave" in view


# -- goals + artifacts sections -----------------------------------------------


def test_mind_view_shows_goals(org):
    org.store.add_goal("learn five things about the user", marker=0)
    view = tui_views.mind_view(org)
    assert "goals" in view
    assert "learn five things about the user" in view


def test_mind_view_shows_completed_goals(org):
    org.store.add_goal("understand rain", marker=0)
    org.store.complete_active_goal()
    view = tui_views.mind_view(org)
    assert "done" in view
    assert "understand rain" in view


def test_memory_view_lists_artifacts(org, tmp_path):
    artifacts = org.store.dir_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "diary.md").write_text("dear diary")
    view = tui_views.memory_view(org)
    assert "artifacts" in view
    assert "diary.md" in view


def test_memory_view_without_artifacts_dir(org):
    assert "artifacts" not in tui_views.memory_view(org)


# -- inner tab -----------------------------------------------------------------


def test_inner_view_shows_mental_state(org):
    org.store.arousal = 0.8
    org.store.add(("self", "mood", "curious"), 0.9)
    view = tui_views.inner_view(org)
    assert "mental state" in view
    assert "arousal" in view
    assert "mood: curious" in view


def test_inner_view_without_mental_state(org):
    # BeliefStore ships scalar defaults (arousal 0.3, stress 0.05,
    # rationality 0.5, irrationality 0.2) — they must render.
    view = tui_views.inner_view(org)
    assert "mental state" in view
    for label in ("arousal", "stress", "rationality", "irrationality"):
        assert label in view
    assert "mood" not in view  # mood only appears once believed


def test_inner_view_shows_loop_and_arena(org):
    org.store.activity = {
        "rules_tried": 4, "derivations": 2, "rules_committed": 1,
        "dreams_promoted": 3, "dreams_discarded": 1,
        "llm_calls": 5, "prompt_tokens": 100, "gen_tokens": 50,
        "utterances": 2, "fallbacks": 0,
    }
    view = tui_views.inner_view(org)
    assert "perpetuation loop" in view
    assert "4 questions → 2 derivations" in view
    assert "3 dreams promoted / 1 discarded" in view
    assert "thought arena" in view
    assert "5 llm calls" in view


def test_inner_view_without_activity(org):
    view = tui_views.inner_view(org)
    assert "(no activity yet)" in view


def test_inner_view_shows_pending_proposal(org, tmp_path):
    artifacts = org.store.dir_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "extensions.json").write_text(
        '{"version": 0, "entries": [], "pending": '
        '{"kind": "seed", "text": "what if the rain is curious"}}')
    view = tui_views.inner_view(org)
    assert "pending proposal" in view
    assert "seed: what if the rain is curious" in view
    assert "/approve to apply" in view


def test_inner_view_without_pending_proposal(org):
    view = tui_views.inner_view(org)
    assert "pending proposal" not in view


# -- inner renderable ---------------------------------------------------------


def _render(renderable, width=80):
    console = Console(width=width, force_terminal=False, color_system=None,
                      record=True)
    console.print(renderable)
    return console.export_text()


def test_inner_renderable_shows_mental_state(org):
    org.store.arousal = 0.8
    org.store.add(("self", "mood", "curious"), 0.9)
    text = _render(tui_views.inner_renderable(org))
    assert "mental state" in text
    assert "arousal" in text
    assert "curious" in text


def test_inner_renderable_shows_loop_and_arena(org):
    org.store.activity = {
        "rules_tried": 4, "derivations": 2, "rules_committed": 1,
        "dreams_promoted": 3, "dreams_discarded": 1,
        "llm_calls": 5, "prompt_tokens": 100, "gen_tokens": 50,
        "utterances": 2, "fallbacks": 0,
    }
    text = _render(tui_views.inner_renderable(org))
    assert "perpetuation loop" in text
    assert "4 questions" in text
    assert "2 derivations" in text
    assert "thought arena" in text
    assert "5 llm calls" in text


def test_inner_renderable_shows_pending_proposal(org, tmp_path):
    artifacts = org.store.dir_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "extensions.json").write_text(
        '{"version": 0, "entries": [], "pending": '
        '{"kind": "seed", "text": "what if the rain is curious"}}')
    text = _render(tui_views.inner_renderable(org))
    assert "pending proposal" in text
    assert "seed" in text
    assert "what if the rain is curious" in text


def test_inner_renderable_without_activity_still_shows_state(org):
    text = _render(tui_views.inner_renderable(org))
    assert "mental state" in text
    assert "perpetuation loop" not in text
