import sys
from pathlib import Path

import pytest

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
