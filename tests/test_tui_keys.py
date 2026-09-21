"""Headless regression tests for TUI key handling: Tab must never move
focus off the chat input (typing would be lost), completion must still
work, and F-pane switches must not strand a following Tab."""

import asyncio

from textual.widgets import Button, Input, RichLog, Static, TabbedContent

from conftest import renderable_text, wait_until
from replicanta.tui import (
    CommandHints,
    CommandPalette,
    MutationBanner,
    OrganismApp,
    RenameScreen,
    SlashCommands,
    TabBar,
    Toast,
)


def test_tab_with_prefix_completes_and_keeps_focus(headless_app):
    """Regression: Tab with '/he' must complete to '/help' AND leave focus
    on the input. Without prevent_default, App._on_key ran Screen's
    tab->focus_next after completion, moving focus to ContentTabs."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            inp = app.chat_input
            inp.focus()
            await pilot.pause()
            await pilot.press("slash", "h", "e")
            await pilot.pause()
            assert inp.value == "/he"
            await pilot.press("tab")
            await pilot.pause()
            assert inp.value == "/help"
            assert inp.has_focus, "Tab moved focus off the chat input"

    asyncio.run(check())


def test_tab_on_empty_input_keeps_focus_and_typing_lands(headless_app):
    """Regression: Tab with an empty input must not move focus; the next
    keystrokes land in the input."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            inp = app.chat_input
            inp.focus()
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            assert inp.has_focus, "Tab moved focus off the chat input"
            await pilot.press("a", "b", "c")
            await pilot.pause()
            assert inp.value == "abc", f"typing lost after Tab: {inp.value!r}"

    asyncio.run(check())


def test_f3_then_tab_then_typing_lands(headless_app):
    """Regression: F3 (pane switch) followed by Tab must keep focus in the
    chat input so the next keystrokes are not lost."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            inp = app.chat_input
            inp.focus()
            await pilot.pause()
            await pilot.press("f3")
            await pilot.pause()
            assert inp.has_focus, "F3 left the chat input unfocused"
            await pilot.press("tab")
            await pilot.pause()
            assert inp.has_focus, "Tab after F3 moved focus off the chat input"
            await pilot.press("a", "b", "c")
            await pilot.pause()
            assert inp.value == "abc", f"typing lost after F3+Tab: {inp.value!r}"

    asyncio.run(check())


def test_f7_switches_to_inner_pane(headless_app):
    """F7 must open the inner tab (mental state + perpetuation loop view)
    while keeping focus on the chat input; the inner pane must exist."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            assert tabs.active == "chat-pane", "chat pane not active initially"
            inp = app.chat_input
            inp.focus()
            await pilot.pause()
            await pilot.press("f7")
            await pilot.pause()
            assert tabs.active == "inner-pane", "F7 did not activate the inner pane"
            assert inp.has_focus, "F7 left the chat input unfocused"
            inner_text = renderable_text(app.query_one("#inner", Static))
            assert inner_text, "inner view rendered empty"
            assert "mental state" in inner_text
            await pilot.press("tab")
            await pilot.pause()
            assert inp.has_focus, "Tab after F7 moved focus off the chat input"

    asyncio.run(check())


def test_ctrl_q_binding_exists_and_saves_before_quit(headless_app, monkeypatch):
    """ctrl+q must be bound to quit and action_quit must flush the organism
    before delegating to the default quit behavior."""

    app = headless_app
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
    monkeypatch.setattr(app, "_arm_hard_exit", lambda: armed.update(called=True))
    app.action_quit()
    assert flushed["called"], "action_quit did not flush organism state"
    assert armed["called"], "action_quit did not arm the hard-exit fallback"
    assert quit_called["called"], "action_quit did not call exit"


def test_main_rejects_invalid_org_name(monkeypatch, tmp_path):
    """--org names must pass nursery.NAME_RE before organism_dir is built —
    otherwise '--org ../otherdir' opens a directory outside the nursery."""
    import pytest

    from replicanta import tui

    monkeypatch.setattr("sys.argv", ["replicanta", "--dir", str(tmp_path), "--org", "../evil"])
    with pytest.raises(SystemExit):
        tui.main()


def test_command_hints_filter_on_slash(headless_app):
    """Typing '/' must surface command hints, filtered by the command token."""
    app = headless_app

    async def check():
        async with app.run_test():
            hints = app.query_one(CommandHints)
            hints.update_for("/voi")
            text = renderable_text(hints)
            assert "voice" in text.lower()
            hints.update_for("/chaos ")
            text = renderable_text(hints)
            assert "/chaos 0..1" in text

    asyncio.run(check())


def test_command_palette_fills_input(headless_app):
    """ctrl+p must open the command palette; selecting a command fills the
    chat input with the command name and a trailing space."""
    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            app.action_command_palette()
            assert isinstance(pilot.app.screen, CommandPalette)
            await pilot.app.screen.dismiss("/chaos")
            await pilot.pause()
            assert app.chat_input.value == "/chaos "
            assert app.chat_input.has_focus

    asyncio.run(check())


def test_tab_bar_labels_visible(headless_app):
    """The custom tab bar must expose the main view labels."""
    app = headless_app

    async def check():
        async with app.run_test():
            bar = app.query_one(TabBar)
            labels = [str(b.label) for b in bar.query(Button)]
            assert "Chat" in labels
            assert "Mind" in labels
            assert "Memory" in labels

    asyncio.run(check())


def test_toast_shows_message(headless_app):
    """show_toast must populate the toast widget and make it visible."""
    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            app.show_toast("Camera not found")
            await pilot.pause()
            toast = app.query_one(Toast)
            assert "Camera not found" in renderable_text(toast)

    asyncio.run(check())


def test_mutation_banner_shows_when_pending(headless_app, monkeypatch):
    """A pending patch must surface the mutation approval banner."""
    app = headless_app
    monkeypatch.setattr(
        "replicanta.extensions.registry",
        lambda: {"pending": {"kind": "rule", "why": "test"}},
    )

    async def check():
        async with app.run_test() as pilot:
            app._update_mutation_banner()
            await pilot.pause()
            banner = app.query_one(MutationBanner)
            assert banner.styles.display != "none"
            summary = app.query_one("#mutation-summary", Static)
            assert "rule" in renderable_text(summary).lower()

    asyncio.run(check())


def test_activity_shows_during_response(headless_app, monkeypatch):
    """A response worker must surface an activity indicator in the status bar."""
    import time

    from replicanta import speech, tui

    app = headless_app

    def slow_respond(*a, **k):
        time.sleep(0.2)

    monkeypatch.setattr("replicanta.voice.respond", slow_respond)
    monkeypatch.setattr(speech, "say", lambda text: None)
    monkeypatch.setattr(tui, "speech", speech)

    async def check():
        async with app.run_test() as pilot:
            app._maybe_respond("hi")
            await pilot.pause()
            assert "thinking" in app.activity_text.lower()
            # the worker's time.sleep(0.2) runs off the event loop, so
            # this is a genuine worker wait — poll bounded for it to clear
            await wait_until(lambda: app.activity_text == "", message="activity label to clear")

    asyncio.run(check())


# -- regression: key bindings -------------------------------------------------


def test_f10_binding_quits_not_doom(headless_app):
    """F10 must resolve to a quit action, as the footer and /help document. A
    duplicate doom binding used to shadow it (textual kept the first),
    so F10 opened DOOM and the app never quit."""

    app = headless_app
    f10_bindings = app._bindings.key_to_bindings.get("f10", [])
    actions = [b.action for b in f10_bindings]
    assert any(a in ("quit", "confirm_quit") for a in actions), f10_bindings
    all_actions = {binding.action for bindings in app._bindings.key_to_bindings.values() for binding in bindings}
    assert "doom_toggle" not in all_actions, "F10 doom binding is back"


def test_doom_pane_arrows_drive_doom_not_history(headless_app, monkeypatch):
    """On the DOOM pane, up/down must reach the doom_up/doom_down
    bindings; on_key used to prevent_default them away and browse chat
    history instead. Other panes keep history browsing."""

    app = headless_app
    doom_calls = []
    monkeypatch.setattr(app, "action_doom_up", lambda: doom_calls.append("up"))
    monkeypatch.setattr(app, "action_doom_down", lambda: doom_calls.append("down"))

    async def check():
        async with app.run_test() as pilot:
            app._chat_history = ["an old line"]
            app.action_show_tab("doom-pane")
            await pilot.pause()
            assert app.chat_input.has_focus
            await pilot.press("up")
            await pilot.pause()
            assert doom_calls == ["up"], doom_calls
            assert app.chat_input.value == ""
            assert app._history_index == -1, "chat history was browsed on the doom pane"
            await pilot.press("down")
            await pilot.pause()
            assert doom_calls == ["up", "down"], doom_calls
            # off the doom pane, up/down browse chat history again
            app.action_show_tab("chat-pane")
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert app.chat_input.value == "an old line"
            assert doom_calls == ["up", "down"], doom_calls

    asyncio.run(check())


# -- regression: modal inputs must not leak into chat --------------------------


def test_modal_input_submitted_does_not_run_chat(headless_app, monkeypatch):
    """Enter in the rename prompt dismisses the prompt — it must not
    also clear the chat line, push history, and run handle_chat with
    the modal text (Submitted bubbles up to the app)."""

    app = headless_app
    chats, commands = [], []
    monkeypatch.setattr(app, "handle_chat", chats.append)
    monkeypatch.setattr(app, "handle_command", commands.append)

    async def check():
        async with app.run_test() as pilot:
            app.chat_input.value = "kept draft"
            app.push_screen(RenameScreen("default"))
            await pilot.pause()
            assert isinstance(app.screen, RenameScreen)
            app.screen.query_one("#rename-input", Input).value = "bracken"
            await pilot.press("enter")
            await pilot.pause()
            assert not chats, f"modal text ran as chat: {chats}"
            assert not commands, f"modal text ran as command: {commands}"
            assert app.chat_input.value == "kept draft", "modal submit cleared the chat line"

    asyncio.run(check())


def test_modal_input_changed_ignores_chat_state(headless_app, monkeypatch):
    """Typing in a modal prompt must not reset completion state or fire
    typing-activity side effects; the chat input itself still does."""

    app = headless_app
    touched = {"n": 0}
    monkeypatch.setattr(app, "_touch_typing", lambda: touched.__setitem__("n", touched["n"] + 1))

    async def check():
        async with app.run_test() as pilot:
            chat = app.chat_input  # capture before the modal takes the screen
            app._completion_matches = ["/kept"]
            app.push_screen(RenameScreen("default"))
            await pilot.pause()
            # drive the real mounted widgets: value assignment posts a
            # genuine Input.Changed that bubbles to the app
            app.screen.query_one("#rename-input", Input).value = "x"
            await pilot.pause()
            assert app._completion_matches == ["/kept"], "modal typing reset completion state"
            assert touched["n"] == 0, "modal typing fired typing activity"
            chat.value = "hi"
            await pilot.pause()
            assert touched["n"] == 1, "chat typing lost its typing activity"
            assert app._completion_matches is None, "chat typing kept stale completion state"

    asyncio.run(check())


# -- regression: command palette -----------------------------------------------


def test_palette_no_arg_command_runs(headless_app):
    """Picking a no-arg command must actually run it. Input.action_submit()
    is async in Textual 8, so calling it synchronously discarded the
    coroutine; SlashCommands posts Input.Submitted instead."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            SlashCommands(pilot.app.screen)._run("/stats")
            await pilot.pause()
            lines = [str(line.text) for line in app.query_one("#dreams", RichLog).lines]
            assert any("stats: beliefs=" in line for line in lines), lines
            assert app.chat_input.value == "", "palette pick left the command in the input"

    asyncio.run(check())


def test_palette_arg_command_only_fills_input(headless_app):
    """Commands with placeholder args still only fill the chat line —
    submitting the placeholder literally would do nonsense."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            SlashCommands(pilot.app.screen)._run("/chaos 0..1")
            await pilot.pause()
            assert app.chat_input.value == "/chaos 0..1"
            assert app.chat_input.has_focus

    asyncio.run(check())


# -- regression: tab completion cycling ----------------------------------------


def test_tab_completion_cycles_all_matches(headless_app):
    """Consecutive Tabs must cycle through every match for the typed
    token; recomputing from the just-completed word used to collapse
    the match set to one."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            inp = app.chat_input
            inp.focus()
            await pilot.pause()
            await pilot.press("slash", "c")
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            first = inp.value
            await pilot.press("tab")
            await pilot.pause()
            second = inp.value
            await pilot.press("tab")
            await pilot.pause()
            third = inp.value
            assert {first, second} == {"/chaos", "/camera"}, (first, second)
            assert third == first, (first, second, third)

    asyncio.run(check())


def test_tab_completion_resets_after_typing(headless_app):
    """Editing the line after a completion recomputes candidates from
    the new token instead of cycling the stale list."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            inp = app.chat_input
            inp.focus()
            await pilot.pause()
            await pilot.press("slash", "c")
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            assert inp.value == "/chaos"
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            assert inp.value == "/chaosa", "Tab changed a token with no matches"

    asyncio.run(check())


# -- regression: mutation banner buttons ---------------------------------------


def test_mutation_banner_buttons_use_extensions_path(headless_app, monkeypatch):
    """Approve/reject must pass the organism's extensions.json path —
    self.org.extension_path does not exist and crashed on_button_pressed."""

    from textual.widgets import Button

    from replicanta import extensions

    app = headless_app
    expected = app.org.dir_path / "artifacts" / "extensions.json"
    calls = []
    monkeypatch.setattr(
        extensions,
        "approve",
        lambda path: calls.append(("approve", path)) or {"kind": "rule"},
    )
    monkeypatch.setattr(
        extensions,
        "reject",
        lambda path: calls.append(("reject", path)) or {"kind": "rule"},
    )
    monkeypatch.setattr(extensions, "registry", lambda: {"pending": {"kind": "rule", "why": "t"}})

    async def check():
        async with app.run_test():
            app.on_button_pressed(Button.Pressed(Button(id="mutation-approve")))
            app.on_button_pressed(Button.Pressed(Button(id="mutation-reject")))
            assert calls == [("approve", expected), ("reject", expected)], calls

    asyncio.run(check())
