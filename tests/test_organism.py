import shutil
import subprocess
from pathlib import Path

from replicanta.organism import Mind, Organism
from replicanta.probe import SystemProbe

SCL = Path(__file__).parent.parent / "organism.scl"


def _dummy_probe():
    return SystemProbe(proc=Path("/nonexistent"), sys=Path("/nonexistent"))


def _seed_organism(tmp_path):
    shutil.copy(SCL, tmp_path / "organism.scl")


def test_mind_loads_seed_and_reads_beliefs():
    mind = Mind(SCL)
    mind.rebuild()
    beliefs = mind.beliefs()
    assert ("self", "mood", "calm") in beliefs
    assert beliefs[("self", "mood", "calm")] > 0.5


def test_mind_beliefs_returns_float_confidences():
    mind = Mind(SCL)
    mind.rebuild()
    for conf in mind.beliefs().values():
        assert isinstance(conf, float)
        assert 0.0 <= conf <= 1.0


import pytest

from replicanta.organism import (  # noqa: F401
    CHAT_LOG_LIMIT,
    VALID_VALUE_RE,
    BeliefStore,
)


@pytest.fixture
def store(tmp_path):
    return BeliefStore(tmp_path)


def test_add_new_belief(store):
    store.add(("apple", "color", "red"), 0.8)
    assert store.conf(("apple", "color", "red")) == 0.8


def test_strengthen_keeps_max(store):
    store.add(("apple", "color", "red"), 0.6)
    store.add(("apple", "color", "red"), 0.9)
    assert store.conf(("apple", "color", "red")) == 0.9


def test_contradiction_prunes_lower_confidence(store):
    store.add(("apple", "color", "red"), 0.9)
    store.add(("apple", "color", "green"), 0.6)
    assert store.conf(("apple", "color", "red")) == 0.9
    assert ("apple", "color", "green") not in store.beliefs()
    assert ("apple", "color", "green") in store.archived()


def test_invalid_value_rejected(store):
    with pytest.raises(ValueError):
        store.add(("apple", "color", "Not Valid!"), 0.9)


def test_render_scl_matches_import_file_format(store):
    store.add(("apple", "color", "red"), 0.9)
    scl = store.render_scl()
    assert 'rel 0.9::bel("apple", "color", "red")' in scl


def test_save_load_roundtrip(store):
    store.add(("apple", "color", "red"), 0.9)
    store.chaos = 0.7
    store.save()
    loaded = BeliefStore(store.dir_path)
    loaded.load()
    assert loaded.conf(("apple", "color", "red")) == 0.9
    assert loaded.chaos == 0.7


def test_scl_with_committed_rules_reimports(tmp_path):
    store = BeliefStore(tmp_path)
    store.add(("apple", "color", "red"), 0.9)
    store.add(("apple", "shape", "round"), 0.8)
    store.rules.append(('q1(x) = bel(x, "color", "red"), bel(x, "shape", "round")', 1))
    store.save()
    mind = Mind(tmp_path / "organism.scl")
    mind.rebuild()
    assert mind.beliefs()[("apple", "color", "red")] == 0.9


def test_mind_derive_runs_transient_rule(tmp_path):
    # Seed the genome directly so two conflicting values survive in bel.
    (tmp_path / "organism.scl").write_text(
        'rel 0.9::bel("apple", "color", "red")\n'
        'rel 0.9::bel("apple", "color", "green")\n'
    )
    mind = Mind(tmp_path / "organism.scl")
    mind.rebuild()
    results = mind.derive(
        "contradicts",
        "contradicts(o, a) = bel(o, a, v1) and bel(o, a, v2) and v1 != v2",
    )
    assert any(tup == ("apple", "color") for _tag, tup in results)


def test_belief_store_derived_flags(tmp_path):
    store = BeliefStore(tmp_path)
    store.add(("self", "is_a", "organism"), 0.9)
    store.add(("self", "mood", "calm"), 0.9)
    store.save()
    mind = Mind(tmp_path / "organism.scl")
    mind.rebuild()
    store.mind = mind
    assert store.derived()["needs_user"] is True
    assert store.derived()["contradictions"] == []
    store.add(("user", "name", "sam"), 0.9)
    assert store.derived()["needs_user"] is False


def test_belief_store_derived_contradictions(tmp_path):
    store = BeliefStore(tmp_path)
    store.add(("self", "is_a", "organism"), 0.9)
    # Seed contradictory facts at equal confidence so neither is auto-archived
    store.beliefs_map[("apple", "color", "red")] = 0.9
    store.beliefs_map[("apple", "color", "green")] = 0.9
    derived = store.derived()
    assert len(derived["contradictions"]) == 1
    assert derived["contradictions"][0]["obj"] == "apple"
    assert derived["contradictions"][0]["attr"] == "color"


def test_record_chat_strips_and_skips_empty(store):
    store.record_chat("user", "  hi there  ")
    store.record_chat("org", "   ")
    assert store.chat_log == [["user", "hi there"]]


def test_record_chat_appends_entries(store):
    store.record_chat("user", "hello")
    store.record_chat("org", "hi back")
    assert store.chat_log == [["user", "hello"], ["org", "hi back"]]


def test_record_chat_caps_log(store):
    for i in range(CHAT_LOG_LIMIT + 10):
        store.record_chat("user", f"line {i}")
    assert len(store.chat_log) == CHAT_LOG_LIMIT
    assert store.chat_log[-1] == ["user", f"line {CHAT_LOG_LIMIT + 9}"]


def test_chat_log_roundtrips_via_save_load(store):
    store.record_chat("user", "hello")
    store.record_chat("org", "hi back")
    store.save()
    loaded = BeliefStore(store.dir_path)
    loaded.load()
    assert loaded.chat_log == [["user", "hello"], ["org", "hi back"]]


from replicanta.organism import AttentionWindow, ChaosKnob


def test_chaos_knob_clamps():
    knob = ChaosKnob()
    knob.set(1.5)
    assert knob.value == 1.0
    knob.set(-0.2)
    assert knob.value == 0.0
    knob.set(0.7)
    assert knob.value == 0.7


def test_attention_window_from_beliefs():
    beliefs = {("apple", "color", "red"): 0.9, ("ball", "shape", "round"): 0.8}
    win = AttentionWindow(beliefs)
    win.refresh()
    assert ("color", "red") in win.pairs
    assert ("shape", "round") in win.pairs


def test_attention_window_narrows_with_fatigue():
    beliefs = {("o1", f"attr{i}", f"val{i}"): 0.9 for i in range(20)}
    win = AttentionWindow(beliefs)
    win.refresh(cycle=1)
    wide = len(win.pairs)
    win.refresh(cycle=10)
    narrow = len(win.pairs)
    assert narrow < wide
    assert narrow >= 3


def test_focus_steering():
    beliefs = {("apple", "color", "red"): 0.9, ("ball", "shape", "round"): 0.8}
    win = AttentionWindow(beliefs)
    win.focus("color")
    assert set(win.pairs) == {("color", "red")}


def test_focus_clears():
    beliefs = {("apple", "color", "red"): 0.9}
    win = AttentionWindow(beliefs)
    win.focus("color")
    win.focus(None)
    win.refresh()
    assert ("color", "red") in win.pairs


from replicanta.organism import SelfQuestioner


def _make_questioner(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text(
        'rel 0.9::bel("apple", "color", "red")\n'
        'rel 0.8::bel("apple", "shape", "round")\n'
        'rel 0.7::bel("ball", "color", "red")\n'
    )
    from replicanta.organism import BeliefStore

    store = BeliefStore(tmp_path)
    store.load()
    store.beliefs_map = {
        ("apple", "color", "red"): 0.9,
        ("apple", "shape", "round"): 0.8,
        ("ball", "color", "red"): 0.7,
    }
    mind = Mind(scl)
    mind.rebuild()
    return SelfQuestioner(store, mind, tmp_path)


def test_derives_new_belief(tmp_path):
    q = _make_questioner(tmp_path)
    # apple is red AND round -> new belief apple has "red+round" = true
    q.ask(("color", "red"), ("shape", "round"))
    assert q.store.conf(("apple", "red_round", "true")) is not None


def test_no_derivation_no_growth(tmp_path):
    q = _make_questioner(tmp_path)
    # nothing is red AND drinkable -> no new belief
    q.ask(("color", "red"), ("drinkable", "true"))
    assert len(q.store.beliefs()) == 3


def test_consolidation_strengthens(tmp_path):
    q = _make_questioner(tmp_path)
    q.ask(("color", "red"), ("shape", "round"))
    before = q.store.conf(("apple", "red_round", "true"))
    q.ask(("color", "red"), ("shape", "round"))
    after = q.store.conf(("apple", "red_round", "true"))
    assert after >= before


def test_chaos_generalization_commits_rule_with_depth(monkeypatch, tmp_path):
    q = _make_questioner(tmp_path)
    q.store.chaos = 1.0
    q.store.rules.append(('q0(x) = bel(x, "color", "red")', 1))
    monkeypatch.setattr(random, "random", lambda: 0.0)
    q.ask(("color", "red"), ("shape", "round"))
    rule, depth = q.store.rules[-1]
    assert depth == 2
    assert rule.startswith("q1(x)")


import random

from replicanta.organism import DreamEngine


def _make_dreamer(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text(
        'rel 0.9::bel("apple", "color", "red")\n'
        'rel 0.8::bel("apple", "shape", "round")\n'
        'rel 0.7::bel("ball", "color", "red")\n'
        'rel 0.9::bel("ball", "shape", "round")\n'
    )
    from replicanta.organism import BeliefStore

    store = BeliefStore(tmp_path)
    store.load()
    store.beliefs_map = {
        ("apple", "color", "red"): 0.9,
        ("apple", "shape", "round"): 0.8,
        ("ball", "color", "red"): 0.7,
        ("ball", "shape", "round"): 0.9,
    }
    mind = Mind(scl)
    mind.rebuild()
    return DreamEngine(store, mind)


def test_dream_generates_candidate_facts(tmp_path):
    engine = _make_dreamer(tmp_path)
    engine.rng = random.Random(42)
    dreams = engine.dream(count=3)
    assert len(dreams) == 3
    for d in dreams:
        assert isinstance(d, dict)
        assert "rule" in d and "combo" in d


def test_dream_validates_and_promotes(tmp_path):
    engine = _make_dreamer(tmp_path)
    engine.rng = random.Random(42)
    dreams = engine.dream(count=5)
    promoted = engine.promote(dreams)
    # at least one dream should promote (apple/ball share color+shape)
    assert len(promoted) >= 1


def test_dream_discards_unsupported(tmp_path):
    engine = _make_dreamer(tmp_path)
    unsupported = [
        {
            "rule": 'q99(x) = bel(x, "color", "red"), bel(x, "drinkable", "true")',
            "combo": "red_true",
            "head": "q99",
        }
    ]
    promoted = engine.promote(unsupported)
    assert promoted == []


from replicanta.organism import Lifecycle, Metrics


def test_lifecycle_advances_cycle(monkeypatch, tmp_path):
    store = BeliefStore(tmp_path)
    lc = Lifecycle(store, wake_seconds=0, sleep_seconds=0)
    lc.tick()  # forces wake -> sleep transition
    assert store.cycle == 1
    assert lc.state in ("sleep", "wake")


def _critical_store(tmp_path):
    store = BeliefStore(tmp_path)
    store.stress = 0.96  # above Lifecycle.FADE_STRESS
    return store


def test_lifecycle_fades_under_sustained_critical_stress(tmp_path):
    store = _critical_store(tmp_path)
    lc = Lifecycle(store, wake_seconds=0, sleep_seconds=0)
    for _ in range(Lifecycle.FADE_LIMIT):
        lc.tick()
    assert lc.state == "dead"
    assert store.fade_streak == Lifecycle.FADE_LIMIT


def test_lifecycle_fade_streak_resets_on_recovery(tmp_path):
    store = _critical_store(tmp_path)
    lc = Lifecycle(store, wake_seconds=0, sleep_seconds=0)
    lc.tick()  # streak 1
    lc.tick()  # streak 2
    assert store.fade_streak == 2
    store.stress = 0.1  # below FADE_STRESS: streak resets
    lc.tick()
    assert store.fade_streak == 0
    assert lc.state != "dead"


def test_lifecycle_dead_is_terminal(tmp_path):
    store = _critical_store(tmp_path)
    lc = Lifecycle(store, wake_seconds=0, sleep_seconds=0)
    for _ in range(Lifecycle.FADE_LIMIT):
        lc.tick()
    assert lc.state == "dead"
    cycle_before = store.cycle
    assert lc.tick() == "dead"  # no further transitions
    assert lc.advance() is None  # scheduler no-op
    assert not lc.due()
    assert store.cycle == cycle_before


def test_revive_restores_wake_baseline(tmp_path):
    store = _critical_store(tmp_path)
    lc = Lifecycle(store, wake_seconds=0, sleep_seconds=0)
    for _ in range(Lifecycle.FADE_LIMIT):
        lc.tick()
    assert lc.state == "dead"
    lc.revive()
    assert lc.state == "wake"
    assert store.fade_streak == 0
    assert store.stress == 0.05  # StressMeter.BASELINE


def test_metrics_score_components(tmp_path):
    store = BeliefStore(tmp_path)
    store.beliefs_map = {("a", "color", "red"): 0.9, ("b", "shape", "round"): 0.8}
    store.rules = [('q1(x) = bel(x, "color", "red")', 1)]
    m = Metrics(store)
    assert m.belief_count == 2
    assert m.rule_count == 1
    assert m.score() > 0


def test_metrics_score_monotonic_under_prune_archive(tmp_path):
    store = BeliefStore(tmp_path)
    store.add(("apple", "color", "red"), 0.9)
    store.add(("apple", "color", "green"), 0.6)
    m1 = Metrics(store).score()
    store.add(("ball", "shape", "round"), 0.8)
    m2 = Metrics(store).score()
    assert m2 >= m1


def _seeded_organism(tmp_path):
    scl = tmp_path / "organism.scl"
    scl.write_text(
        'rel 0.9::bel("apple", "color", "red")\n'
        'rel 0.8::bel("apple", "shape", "round")\n'
        'rel 0.7::bel("ball", "color", "red")\n'
        'rel 0.9::bel("ball", "shape", "round")\n'
    )
    return Organism(tmp_path, wake_seconds=0, sleep_seconds=0)


def test_organism_bootstraps_from_genome(tmp_path):
    org = _seeded_organism(tmp_path)
    org.load()
    assert org.store.conf(("apple", "color", "red")) == 0.9
    assert org.store.conf(("ball", "shape", "round")) == 0.9
    assert org.metrics().score() > 0


def test_organism_sleeps_and_grows(tmp_path):
    org = _seeded_organism(tmp_path)
    org.load()
    score_before = org.metrics().score()
    org.advance_cycle()
    score_after = org.metrics().score()
    assert score_after >= score_before
    assert org.store.cycle >= 1


def test_organism_self_play_grows_over_cycles(tmp_path):
    org = _seeded_organism(tmp_path)
    org.load()
    scores = [org.metrics().score()]
    for _ in range(5):
        org.advance_cycle()
        scores.append(org.metrics().score())
    assert scores[-1] >= scores[0]


def test_organism_death_persists_across_reload(tmp_path):
    org = _seeded_organism(tmp_path)
    org.load()
    org.store.stress = 0.96
    for _ in range(Lifecycle.FADE_LIMIT):
        org.lifecycle.tick()
    assert org.lifecycle.state == "dead"
    org.store.save()
    org2 = _seeded_organism(tmp_path)
    org2.load()
    assert org2.lifecycle.state == "dead"


from replicanta.tui import OrganismApp


def test_tui_app_constructs(tmp_path):
    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    assert app is not None


def test_tui_command_chaos(tmp_path):
    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    app.handle_command("/chaos 0.8")
    assert org.store.chaos == 0.8


def test_tui_command_focus(tmp_path):
    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    app.handle_command("/focus color")
    assert org.window.focus_attr == "color"


def test_tui_command_revive_brings_back_dead(monkeypatch, tmp_path):
    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    org.store.stress = 0.96
    for _ in range(Lifecycle.FADE_LIMIT):
        org.lifecycle.tick()
    assert org.lifecycle.state == "dead"
    app = OrganismApp(org)

    class FakeStatic:
        def update(self, *a, **k):
            pass

        def write(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: FakeStatic())
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
    app.handle_command("/revive")
    assert org.lifecycle.state == "wake"
    assert org.store.fade_streak == 0
    assert org.store.stress == 0.05


def test_tui_command_self_talk_toggles(monkeypatch, tmp_path):
    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    app._maybe_self_talk = lambda: None  # avoid worker threads in test

    class FakeStatic:
        def update(self, *a, **k):
            pass

        def write(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: FakeStatic())
    app.handle_command("/self-talk")
    assert app._self_talk_on is True
    app.handle_command("/self-talk")
    assert app._self_talk_on is False


def test_tui_narrate_routes_to_self_talk_when_on(tmp_path):
    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    calls = []
    app._self_talk_on = True
    app._maybe_self_talk = lambda: calls.append("self_talk")
    app._narrate = lambda: calls.append("narrate")
    app._narrating = False
    app._maybe_narrate()
    assert calls == ["self_talk"]


def test_tui_narrate_stays_dream_when_sleeping(tmp_path):
    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    calls = []
    app._self_talk_on = True
    org.lifecycle.transition("sleep")
    app.refresh_status = lambda: None
    app._maybe_self_talk = lambda: calls.append("self_talk")
    app._narrate = lambda: calls.append("narrate")
    app._narrating = False
    app._maybe_narrate()
    assert calls == ["narrate"]


def test_tui_focuses_chat_input_on_mount(monkeypatch, tmp_path):
    """Regression: the cursor must start in the chat line, not the
    scrollable log pane (Textual otherwise focuses the first focusable
    widget, which is the log)."""
    import asyncio

    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    monkeypatch.setattr(app, "_probe_voice", lambda: None)
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)

    async def check():
        async with app.run_test():
            assert app.chat_input.has_focus

    asyncio.run(check())


# -- multi-screen UX ---------------------------------------------------------


def _headless_app(monkeypatch, tmp_path):
    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    monkeypatch.setattr(app, "_probe_voice", lambda: None)
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
    return app


def test_tui_has_four_tabs(monkeypatch, tmp_path):
    import asyncio

    from textual.widgets import TabbedContent, TabPane

    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            tabs = app.query_one(TabbedContent)
            assert {p.id for p in tabs.query(TabPane)} == {
                "chat-pane",
                "mind-pane",
                "memory-pane",
                "inner-pane",
                "cells-pane",
                "mud-pane",
            }

    asyncio.run(check())


def test_tui_pending_widget_streams_and_hides(monkeypatch, tmp_path):
    import asyncio

    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            app._pending_show("org is thinking")
            assert app._pending_visible
            app._pending_token("hel")
            app._pending_token("lo")
            assert app._pending_text == "hello"
            app._pending_hide()
            assert not app._pending_visible

    asyncio.run(check())


def test_tui_status_bar_uses_words(monkeypatch, tmp_path):
    import asyncio

    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            app.refresh_status()
            assert "beliefs" in app._bottombar_text
            assert "rules" in app._bottombar_text
            assert "inner voice" in app._bottombar_text

    asyncio.run(check())


def test_tui_mind_and_memory_views_refresh(monkeypatch, tmp_path):
    import asyncio

    app = _headless_app(monkeypatch, tmp_path)
    app.org.store.add(("user", "name", "sam"), 0.8)
    app.org.store.remember("learned", "your name is sam")

    async def check():
        async with app.run_test():
            app._refresh_views()
            assert "beliefs" in app._mind_text
            assert "your name is sam" in app._memory_text

    asyncio.run(check())


def test_tui_chat_renders_as_card(monkeypatch, tmp_path):
    from rich.panel import Panel

    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    written = []

    class Rec:
        def write(self, r, *a, **k):
            written.append(r)

        def update(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app._log_chat("user", "hello there")
    panels = [w for w in written if isinstance(w, Panel)]
    assert len(panels) == 1
    assert panels[0].renderable.plain == "hello there"
    assert "you" in str(panels[0].title)
    # a blank spacer precedes the card so exchanges breathe
    assert written[0] == ""


def test_tui_org_reply_renders_as_card(monkeypatch, tmp_path):
    from rich.panel import Panel

    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    written = []

    class Rec:
        def write(self, r, *a, **k):
            written.append(r)

        def update(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app._set_reply("I am here.")
    panels = [w for w in written if isinstance(w, Panel)]
    assert len(panels) == 1
    assert panels[0].renderable.plain == "I am here."
    assert "org" in str(panels[0].title)


def test_tui_events_stay_flat_lines(monkeypatch, tmp_path):
    from rich.panel import Panel

    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    written = []

    class Rec:
        def write(self, r, *a, **k):
            written.append(r)

        def update(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app._render_event({"kind": "learned", "text": "your name is sam"})
    assert not [w for w in written if isinstance(w, Panel)]


def test_tui_org_card_titled_by_dir_name_by_default(monkeypatch, tmp_path):
    from rich.panel import Panel

    from replicanta.organism import Organism

    org = Organism(tmp_path / "organisms" / "fern")
    org.load()
    app = OrganismApp(org)
    written = []

    class Rec:
        def write(self, r, *a, **k):
            written.append(r)

        def update(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app._log_chat("org", "hi")
    panels = [w for w in written if isinstance(w, Panel)]
    assert "fern" in str(panels[0].title)


def test_tui_org_card_uses_learned_name(monkeypatch, tmp_path):
    from rich.panel import Panel

    from replicanta.organism import Organism

    org = Organism(tmp_path)
    org.load()
    org.store.add(("self", "name", "sprig"), 0.8)
    app = OrganismApp(org)
    written = []

    class Rec:
        def write(self, r, *a, **k):
            written.append(r)

        def update(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app._log_chat("org", "hi")
    panels = [w for w in written if isinstance(w, Panel)]
    assert "sprig" in str(panels[0].title)


# -- goals + artifacts wiring ------------------------------------------------


def test_tui_want_goal_routes_to_form_goal(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    calls = []
    app._form_goal = lambda: calls.append("form")
    app._render_event({"kind": "want_goal"})
    assert calls == ["form"]


def test_tui_want_diary_routes_to_write_diary(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    calls = []
    app._write_diary = lambda: calls.append("write")
    app._render_event({"kind": "want_diary"})
    assert calls == ["write"]


def test_tui_goal_event_logs_completion(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    logged = []
    app._append_log = lambda text, style=None, stamp=False: logged.append(text)
    app.notify = lambda *a, **k: None
    app._render_event({"kind": "goal", "text": "learn about the user", "done": True})
    assert any("learn about the user" in line for line in logged)


def test_tui_set_goal_adds_and_logs(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    logged = []

    class Rec:
        def write(self, r, *a, **k):
            pass

        def update(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app._append_log = lambda text, style=None, stamp=False: logged.append(text)
    app.notify = lambda *a, **k: None
    app.refresh_status = lambda: None
    app._set_goal("learn five things about the user")
    goal = app.org.store.active_goal()
    assert goal is not None
    assert goal["text"] == "learn five things about the user"
    assert any("goal" in line for line in logged)


def test_tui_set_diary_writes_file_and_logs(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    logged = []

    class Rec:
        def write(self, r, *a, **k):
            pass

        def update(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app._append_log = lambda text, style=None, stamp=False: logged.append(text)
    app.refresh_status = lambda: None
    app._set_diary("today I learned about rain.")
    diary = tmp_path / "artifacts" / "diary.md"
    assert "today I learned about rain." in diary.read_text()
    assert any("diary" in line for line in logged)


def test_tui_want_reflect_routes_to_reflect_worker(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    calls = []
    app._reflect = lambda: calls.append("reflect")
    app._render_event({"kind": "want_reflect"})
    assert calls == ["reflect"]


def test_tui_set_reflection_logs_created_skill(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    logged = []

    class Rec:
        def write(self, r, *a, **k):
            pass

        def update(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app._append_log = lambda text, style=None, stamp=False: logged.append(text)
    app.notify = lambda *a, **k: None
    app.refresh_status = lambda: None
    app._set_reflection({"action": "created", "name": "rain talk"})
    assert any("rain talk" in line for line in logged)
    assert any(m["kind"] == "skill" for m in app.org.store.memory)


def test_tui_set_reflection_nothing_is_quiet(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    logged = []

    class Rec:
        def write(self, r, *a, **k):
            pass

        def update(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app._append_log = lambda text, style=None, stamp=False: logged.append(text)
    app._set_reflection({"action": "none"})
    assert logged == []


# -- tier B: approval commands ---------------------------------------------------

from replicanta import extensions as ext_mod


def _proposal_entry():
    return {
        "kind": "pattern",
        "regex": "i adore ([a-z '-]+)",
        "template": "user:like_{x}:true",
        "example": "i adore hiking",
        "why": "the user says adore",
    }


def _patch_app(monkeypatch, app, logged):
    class Rec:
        def write(self, r, *a, **k):
            pass

        def update(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app._append_log = lambda text, style=None, stamp=False: logged.append(text)
    app.notify = lambda *a, **k: None


def test_tui_set_reflection_proposal_shows_card(monkeypatch, tmp_path):
    from rich.panel import Panel

    app = _headless_app(monkeypatch, tmp_path)
    written = []

    class Rec:
        def write(self, r, *a, **k):
            written.append(r)

        def update(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app.notify = lambda *a, **k: None
    app.refresh_status = lambda: None
    app._set_reflection({"action": "proposal", "entry": _proposal_entry()})
    panels = [w for w in written if isinstance(w, Panel)]
    assert panels and "proposes a patch" in str(panels[0].title)
    assert any(m["kind"] == "skill" for m in app.org.store.memory)


def test_tui_approve_applies_pending(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    app.org.store.auto_apply_patches = False
    logged = []
    _patch_app(monkeypatch, app, logged)
    ext_mod.propose(
        app.org.dir_path / "artifacts" / "extensions.json",
        _proposal_entry(),
        auto_apply=False,
    )
    app.handle_command("/approve")
    assert ext_mod.active_entries("pattern")[0]["regex"] == "i adore ([a-z '-]+)"
    assert any("applied" in line for line in logged)


def test_tui_approve_without_pending(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    app.org.store.auto_apply_patches = False
    logged = []
    _patch_app(monkeypatch, app, logged)
    app.handle_command("/approve")
    assert any("no pending" in line for line in logged)


def test_tui_reject_discards_pending(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    app.org.store.auto_apply_patches = False
    logged = []
    _patch_app(monkeypatch, app, logged)
    ext_mod.propose(
        app.org.dir_path / "artifacts" / "extensions.json",
        _proposal_entry(),
        auto_apply=False,
    )
    app.handle_command("/reject")
    assert ext_mod.pending() is None
    assert ext_mod.active_entries("pattern") == []
    assert any("rejected" in line for line in logged)


def test_tui_revert_removes_last_patch(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    logged = []
    _patch_app(monkeypatch, app, logged)
    ext_mod.propose(
        app.org.dir_path / "artifacts" / "extensions.json",
        _proposal_entry(),
        auto_apply=False,
    )
    ext_mod.approve(app.org.dir_path / "artifacts" / "extensions.json")
    app.handle_command("/revert")
    assert ext_mod.active_entries("pattern") == []
    assert any("reverted" in line for line in logged)


def test_tui_auto_apply_patches_default_on(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    assert app.org.store.auto_apply_patches is True


def test_tui_auto_apply_toggle(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    logged = []
    _patch_app(monkeypatch, app, logged)
    app.handle_command("/auto-apply off")
    assert not app.org.store.auto_apply_patches
    assert any("auto-apply patches: off" in line for line in logged)
    app.handle_command("/auto-apply on")
    assert app.org.store.auto_apply_patches
    assert any("auto-apply patches: on" in line for line in logged)


def test_tui_proposal_auto_applies_when_setting_on(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    logged = []
    _patch_app(monkeypatch, app, logged)
    ext_mod.propose(
        app.org.dir_path / "artifacts" / "extensions.json",
        _proposal_entry(),
        auto_apply=True,
    )
    assert ext_mod.active_entries("pattern")[0]["regex"] == "i adore ([a-z '-]+)"


# -- nursery: /new, /swap, /organisms -----------------------------------------


def _nursery_app(monkeypatch, tmp_path):
    """An app whose organism lives in a real nursery: tmp_path is the root
    (with the seed genome), the organism at organisms/default/."""
    from replicanta import nursery
    from replicanta.organism import Organism

    root = tmp_path
    (root / "organism.scl").write_text("type bel(x: String, a: String, v: String)\n")
    nursery.create(root, "default", root / "organism.scl")
    org = Organism(nursery.organism_dir(root, "default"))
    org.load()
    app = OrganismApp(org, root)
    logged = []

    class Rec:
        def write(self, r, *a, **k):
            pass

        def update(self, *a, **k):
            pass

        def clear(self, *a, **k):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: Rec())
    app._append_log = lambda text, style=None, stamp=False: logged.append(text)
    app.notify = lambda *a, **k: None
    return app, root, logged


def test_tui_new_births_and_swaps(monkeypatch, tmp_path):
    from replicanta import nursery

    app, root, logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/new fern")
    assert app.org.dir_path == nursery.organism_dir(root, "fern")
    assert (root / "organisms" / "fern" / "organism.scl").exists()
    assert nursery.current(root) == "fern"
    assert any("now living with fern" in line for line in logged)


def test_tui_new_bare_autonames(monkeypatch, tmp_path):
    app, _root, _logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/new")
    assert app.org.dir_path.name == "replicanta-2"


def test_tui_new_rejects_duplicate(monkeypatch, tmp_path):
    app, _root, logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/new default")
    assert app.org.dir_path.name == "default"  # stayed put
    assert any("already exists" in line for line in logged)


def test_tui_organisms_lists_with_current_marked(monkeypatch, tmp_path):
    app, _root, logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/new fern")
    logged.clear()
    app.handle_command("/organisms")
    assert any("*fern" in line and "default" in line for line in logged)


def test_tui_swap_roundtrip_and_unknown(monkeypatch, tmp_path):
    app, _root, logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/new fern")
    app.handle_command("/swap default")
    assert app.org.dir_path.name == "default"
    logged.clear()
    app.handle_command("/swap nope")
    assert app.org.dir_path.name == "default"
    assert any("no organism 'nope'" in line for line in logged)


def test_tui_swap_works_while_busy(monkeypatch, tmp_path):
    """Swaps are never blocked: in-flight workers belong to the old
    organism (their deliveries are dropped by identity checks), and the
    busy flags reset so the new organism can speak immediately."""
    app, root, _logged = _nursery_app(monkeypatch, tmp_path)
    app._responding = True
    app.handle_command("/new fern")
    assert app.org.dir_path.name == "fern"  # swapped anyway
    assert (root / "organisms" / "fern").exists()
    assert app._responding is False  # flags reset for the new org


from replicanta import speech


def test_tui_voice_toggles_speech(monkeypatch, tmp_path):
    """`/voice` flips the spoken voice; enabling it announces itself with a
    spoken greeting (patched out here)."""
    said = []
    monkeypatch.setattr(speech, "available", lambda: True)
    monkeypatch.setattr(speech, "say", lambda text: said.append(text))
    app, _root, logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/voice on")
    assert speech.enabled is True
    assert said == ["I can speak now."]
    assert any("spoken voice on" in line for line in logged)
    app.handle_command("/voice off")
    assert speech.enabled is False
    assert any("spoken voice off" in line for line in logged)


def test_tui_voice_bare_toggles(monkeypatch, tmp_path):
    monkeypatch.setattr(speech, "available", lambda: True)
    monkeypatch.setattr(speech, "say", lambda text: None)
    app, _root, _logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/voice")
    assert speech.enabled is True
    app.handle_command("/voice")
    assert speech.enabled is False


def test_tui_voice_warns_without_model(monkeypatch, tmp_path):
    monkeypatch.setattr(speech, "available", lambda: False)
    monkeypatch.setattr(speech, "say", lambda text: None)
    app, _root, logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/voice on")
    assert speech.enabled is True  # flag set, but honest about it
    assert any("staying mute" in line for line in logged)


def test_tui_voice_rejects_bad_arg(monkeypatch, tmp_path):
    app, _root, logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/voice loudly")
    assert speech.enabled is False
    assert any("/voice list" in line for line in logged)


def test_tui_voice_list_marks_active(monkeypatch, tmp_path):
    monkeypatch.setattr(
        speech, "list_voices", lambda: ["en_GB-alan-low", "en_US-lessac-medium"]
    )
    monkeypatch.setattr(speech, "voice_name", lambda: "en_US-lessac-medium")
    app, _root, logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/voice list")
    assert any(
        "*en_US-lessac-medium" in line and "en_GB-alan-low" in line for line in logged
    )


def test_tui_voice_use_switches_and_speaks(monkeypatch, tmp_path):
    said = []
    monkeypatch.setattr(speech, "say", lambda text: said.append(text))
    monkeypatch.setattr(speech, "set_voice", lambda spec: Path(f"voices/{spec}.onnx"))
    monkeypatch.setattr(speech, "voice_name", lambda: "en_GB-alan-low")
    app, _root, logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/voice use en_GB-alan-low")
    assert any("voice: en_GB-alan-low" in line for line in logged)
    assert said == ["This is my new voice."]


def test_tui_voice_use_unknown_suggests_get(monkeypatch, tmp_path):
    monkeypatch.setattr(speech, "set_voice", lambda spec: None)
    monkeypatch.setattr(speech, "list_voices", lambda: ["en_US-lessac-medium"])
    app, _root, logged = _nursery_app(monkeypatch, tmp_path)
    app.handle_command("/voice use en_GB-alan-low")
    assert any(
        "no voice 'en_GB-alan-low'" in line and "/voice get en_GB-alan-low" in line
        for line in logged
    )


def test_tui_voice_get_runs_download_worker(monkeypatch, tmp_path):
    got = []
    app, _root, _logged = _nursery_app(monkeypatch, tmp_path)
    app._voice_download = lambda name: got.append(name)
    app.handle_command("/voice get en_GB-alan-low")
    assert got == ["en_GB-alan-low"]


def test_tui_reply_speaks_only_when_enabled(monkeypatch, tmp_path):
    """The organism's own utterances route through speech.say — a no-op
    unless the user turned the spoken voice on."""
    import threading

    said = []
    spoke = threading.Event()

    def fake_speak(text):
        said.append(text)
        spoke.set()

    monkeypatch.setattr(speech, "available", lambda: True)
    monkeypatch.setattr(speech, "_speak", fake_speak)
    app, _root, _logged = _nursery_app(monkeypatch, tmp_path)
    app._set_reply("i am here")
    assert said == []  # disabled: silence
    speech.set_enabled(True)
    app._set_reply("i am here")
    assert spoke.wait(2.0)
    assert said == ["i am here"]


def test_load_tolerates_corrupt_state_json(tmp_path):
    store = BeliefStore(tmp_path)
    store.state_path.parent.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text("{not json")
    store.load()  # must not raise; keeps fresh defaults
    assert store.cycle == 0


def test_organism_git_probe_disabled_by_default(tmp_path):
    _seed_organism(tmp_path)
    org = Organism(tmp_path, probe=_dummy_probe())
    org.load()
    assert org.git_probe is None


def test_organism_git_probe_attached_when_enabled(tmp_path):
    _seed_organism(tmp_path)
    (tmp_path / "replicanta.toml").write_text("[git]\nenabled = true\n")
    org = Organism(tmp_path, probe=_dummy_probe())
    org.load()
    assert org.git_probe is not None


def test_organism_sense_folds_git_beliefs(tmp_path, monkeypatch):
    _seed_organism(tmp_path)
    from replicanta.gitstate import GitProbe

    fake = GitProbe(
        tmp_path,
        spawn=lambda _w, _a: subprocess.CompletedProcess(_a, 0, "true\n", ""),
    )
    monkeypatch.setattr(
        fake,
        "snapshot",
        lambda: {
            "is_repo": True,
            "branch": "main",
            "upstream": "origin/main",
            "dirty_count": 3,
            "unpushed_count": 2,
            "behind_count": 0,
        },
    )
    org = Organism(tmp_path, probe=_dummy_probe(), git_probe=fake)
    org.load()
    org.sense()
    assert ("git", "dirty", "few") in org.store.beliefs()
    assert ("git", "unpushed", "few") in org.store.beliefs()
    assert ("git", "behind", "none") in org.store.beliefs()


def test_organism_git_distress_records_memory(tmp_path, monkeypatch):
    _seed_organism(tmp_path)
    from replicanta.gitstate import GitProbe

    fake = GitProbe(
        tmp_path,
        spawn=lambda _w, _a: subprocess.CompletedProcess(_a, 0, "true\n", ""),
    )
    monkeypatch.setattr(
        fake,
        "snapshot",
        lambda: {
            "is_repo": True,
            "branch": "main",
            "upstream": "origin/main",
            "dirty_count": 3,
            "unpushed_count": 0,
            "behind_count": 0,
        },
    )
    org = Organism(tmp_path, probe=_dummy_probe(), git_probe=fake)
    org.load()
    org.sense()
    assert any("uncommitted" in m.get("text", "") for m in org.store.memory)


def test_organism_git_enable_disable_persist_config(tmp_path):
    _seed_organism(tmp_path)
    from replicanta import config

    org = Organism(tmp_path, probe=_dummy_probe())
    org.load()
    org.git_enable()
    assert org.git_probe is not None
    assert config.load_config(tmp_path)["git"]["enabled"] is True
    org.git_disable()
    assert org.git_probe is None
    assert config.load_config(tmp_path)["git"]["enabled"] is False


def test_organism_git_status(tmp_path, monkeypatch):
    _seed_organism(tmp_path)
    from replicanta.gitstate import GitProbe

    fake = GitProbe(
        tmp_path,
        spawn=lambda _w, _a: subprocess.CompletedProcess(_a, 0, "true\n", ""),
    )
    monkeypatch.setattr(
        fake,
        "snapshot",
        lambda: {
            "is_repo": True,
            "branch": "main",
            "upstream": "origin/main",
            "dirty_count": 3,
            "unpushed_count": 2,
            "behind_count": 1,
        },
    )
    org = Organism(tmp_path, probe=_dummy_probe(), git_probe=fake)
    org.load()
    assert org.git_status() == "main · 3△ · 2↑ · 1↓"


def test_organism_git_status_when_disabled(tmp_path):
    _seed_organism(tmp_path)
    org = Organism(tmp_path, probe=_dummy_probe())
    org.load()
    assert org.git_status() == "git sensing is off"


def test_organism_loads_modules(tmp_path, monkeypatch):
    _seed_organism(tmp_path)
    modules_dir = tmp_path / "modules" / "testmod"
    modules_dir.mkdir(parents=True)
    (modules_dir / "manifest.toml").write_text(
        'name = "testmod"\nversion = "1.0.0"\n'
    )
    (modules_dir / "init.lua").write_text(
        'function init(ctx)\n'
        '  ctx.services.get("commands"):register("/test", function(args) return "ok" end)\n'
        'end\n'
    )
    (tmp_path / "replicanta.toml").write_text('[modules]\nenabled = ["testmod"]\n')
    org = Organism(tmp_path, probe=_dummy_probe())
    org.load()
    result = org.module_loader.registry.get("commands").dispatch("/test", [])
    assert result == "ok"
