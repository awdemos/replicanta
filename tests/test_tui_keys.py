"""Headless regression tests for TUI key handling: Tab must never move
focus off the chat input (typing would be lost), completion must still
work, and F-pane switches must not strand a following Tab."""

import asyncio
import sys
from pathlib import Path

from textual.widgets import TabbedContent

sys.path.insert(0, str(Path(__file__).parent.parent))

from organism import Organism
from tui import OrganismApp


def _headless_app(monkeypatch, tmp_path):
    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    monkeypatch.setattr(app, "_probe_voice", lambda: None)
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
    # _on_tick (1s interval) can fire while run_test() is tearing down the
    # DOM, making _append_log's query_one("#dreams") raise NoMatches.
    monkeypatch.setattr(app, "_on_tick", lambda: None)
    return app


def test_tab_with_prefix_completes_and_keeps_focus(monkeypatch, tmp_path):
    """Regression: Tab with '/he' must complete to '/help' AND leave focus
    on the input. Without prevent_default, App._on_key ran Screen's
    tab->focus_next after completion, moving focus to ContentTabs."""

    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test() as pilot:
            inp = app.chat_input
            inp.focus()
            await asyncio.sleep(0.05)
            await pilot.press("slash", "h", "e")
            await asyncio.sleep(0.05)
            assert inp.value == "/he"
            await pilot.press("tab")
            await asyncio.sleep(0.05)
            assert inp.value == "/help"
            assert inp.has_focus, "Tab moved focus off the chat input"

    asyncio.run(check())


def test_tab_on_empty_input_keeps_focus_and_typing_lands(monkeypatch, tmp_path):
    """Regression: Tab with an empty input must not move focus; the next
    keystrokes land in the input."""

    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test() as pilot:
            inp = app.chat_input
            inp.focus()
            await asyncio.sleep(0.05)
            await pilot.press("tab")
            await asyncio.sleep(0.05)
            assert inp.has_focus, "Tab moved focus off the chat input"
            await pilot.press("a", "b", "c")
            await asyncio.sleep(0.05)
            assert inp.value == "abc", f"typing lost after Tab: {inp.value!r}"

    asyncio.run(check())


def test_f3_then_tab_then_typing_lands(monkeypatch, tmp_path):
    """Regression: F3 (pane switch) followed by Tab must keep focus in the
    chat input so the next keystrokes are not lost."""

    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test() as pilot:
            inp = app.chat_input
            inp.focus()
            await asyncio.sleep(0.05)
            await pilot.press("f3")
            await asyncio.sleep(0.1)
            assert inp.has_focus, "F3 left the chat input unfocused"
            await pilot.press("tab")
            await asyncio.sleep(0.05)
            assert inp.has_focus, "Tab after F3 moved focus off the chat input"
            await pilot.press("a", "b", "c")
            await asyncio.sleep(0.05)
            assert inp.value == "abc", f"typing lost after F3+Tab: {inp.value!r}"

    asyncio.run(check())


def test_f7_switches_to_inner_pane(monkeypatch, tmp_path):
    """F7 must open the inner tab (mental state + perpetuation loop view)
    while keeping focus on the chat input; the inner pane must exist."""

    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            assert tabs.active == "chat-pane", "chat pane not active initially"
            inp = app.chat_input
            inp.focus()
            await asyncio.sleep(0.05)
            await pilot.press("f7")
            await asyncio.sleep(0.1)
            assert tabs.active == "inner-pane", "F7 did not activate the inner pane"
            assert inp.has_focus, "F7 left the chat input unfocused"
            assert app._inner_text, "inner view rendered empty"
            assert "mental state" in app._inner_text
            await pilot.press("tab")
            await asyncio.sleep(0.05)
            assert inp.has_focus, "Tab after F7 moved focus off the chat input"

    asyncio.run(check())


def test_ctrl_q_binding_exists_and_saves_before_quit(monkeypatch, tmp_path):
    """ctrl+q must be bound to quit and action_quit must flush the organism
    before delegating to the default quit behavior."""

    app = _headless_app(monkeypatch, tmp_path)
    keys = list(app._bindings.key_to_bindings.keys())
    assert "ctrl+q" in keys, "ctrl+q quit binding missing"

    flushed = {"called": False}
    original_flush = app.org.flush

    def tracking_flush(*args, **kwargs):
        flushed["called"] = True
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(app.org, "flush", tracking_flush)

    quit_called = {"called": False}

    def fake_exit(_self):
        quit_called["called"] = True

    monkeypatch.setattr(OrganismApp, "exit", fake_exit)
    # stub the hard-exit timer: the real one would os._exit the pytest
    # process two seconds after this test runs
    armed = {"called": False}
    monkeypatch.setattr(
        app, "_arm_hard_exit", lambda: armed.update(called=True))
    app.action_quit()
    assert flushed["called"], "action_quit did not flush organism state"
    assert armed["called"], "action_quit did not arm the hard-exit fallback"
    assert quit_called["called"], "action_quit did not call exit"


def test_main_rejects_invalid_org_name(monkeypatch, tmp_path):
    """--org names must pass nursery.NAME_RE before organism_dir is built —
    otherwise '--org ../otherdir' opens a directory outside the nursery."""
    import pytest
    import tui

    monkeypatch.setattr(
        "sys.argv", ["replicanta", "--dir", str(tmp_path), "--org", "../evil"])
    with pytest.raises(SystemExit):
        tui.main()
