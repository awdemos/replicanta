"""Headless regression tests for TUI key handling: Tab must never move
focus off the chat input (typing would be lost), completion must still
work, and the overlay screens must not strand a following Tab."""

import asyncio

from textual.widgets import Input, RichLog, Static

from conftest import renderable_text, wait_until
from replicanta.tui import (
    BeingScreen,
    CommandPalette,
    DoomScreen,
    MutationBanner,
    OrganismApp,
    RenameScreen,
    Toast,
)


def test_tab_with_prefix_completes_and_keeps_focus(headless_app):
    """Regression: Tab with '/he' must complete to '/help' AND leave focus
    on the input. Without prevent_default, App._on_key ran Screen's
    tab->focus_next after completion, moving focus off the input."""

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
    """Regression: F3 (being overlay) followed by esc and Tab must keep
    focus in the chat input so the next keystrokes are not lost."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            inp = app.chat_input
            inp.focus()
            await pilot.pause()
            await pilot.press("f3")
            await pilot.pause()
            assert isinstance(app.screen, BeingScreen), "F3 did not open the being overlay"
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, BeingScreen), "esc did not close the being overlay"
            assert inp.has_focus, "esc left the chat input unfocused"
            await pilot.press("tab")
            await pilot.pause()
            assert inp.has_focus, "Tab after F3+esc moved focus off the chat input"
            await pilot.press("a", "b", "c")
            await pilot.pause()
            assert inp.value == "abc", f"typing lost after F3+esc+Tab: {inp.value!r}"

    asyncio.run(check())


def test_removed_f_keys_are_unbound_and_f3_opens_being(headless_app):
    """The tabbed panes are gone: F2/F4/F7/F8/shift+F8 must not be bound,
    and F3 must drive the being overlay action."""

    app = headless_app
    keys = app._bindings.key_to_bindings
    for gone in ("f2", "f4", "f7", "f8", "shift+f8"):
        assert gone not in keys, f"{gone} still bound"
    f3_actions = [b.action for b in keys.get("f3", [])]
    assert f3_actions == ["being"], f3_actions
    assert "ctrl+b" in keys


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


def test_ctrl_c_quits_on_first_press(headless_app):
    """Terminal convention: a single ctrl+c exits — no double-tap hint
    machinery (regression: the first press used to only show a toast)."""
    app = headless_app
    actions = [b.action for b in app._bindings.key_to_bindings.get("ctrl+c", [])]
    assert actions == ["quit"], actions
    assert not hasattr(OrganismApp, "action_quit_or_hint")


def test_main_rejects_invalid_org_name(monkeypatch, tmp_path):
    """--org names must pass nursery.NAME_RE before organism_dir is built —
    otherwise '--org ../otherdir' opens a directory outside the nursery."""
    import pytest

    from replicanta import tui

    monkeypatch.setattr("sys.argv", ["replicanta", "--dir", str(tmp_path), "--org", "../evil"])
    with pytest.raises(SystemExit):
        tui.main()


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


def test_doom_arrows_drive_doom_only_on_the_overlay(headless_app, monkeypatch):
    """Arrows must reach the game only while the DoomScreen overlay is up
    (now via DoomScreen's own bindings); everywhere else they browse chat
    history — the app no longer special-cases them at all."""

    app = headless_app
    doom_calls = []
    monkeypatch.setattr(app._doom, "key_command", lambda cmd: doom_calls.append(cmd))

    async def check():
        async with app.run_test() as pilot:
            app._chat_history = ["an old line"]
            # off the overlay: up browses chat history, no doom command
            await pilot.press("up")
            await pilot.pause()
            assert app.chat_input.value == "an old line"
            assert doom_calls == [], doom_calls
            await pilot.press("down")
            await pilot.pause()
            assert app.chat_input.value == ""
            assert doom_calls == [], doom_calls
            # on the overlay: the arrows drive the game, not the history
            app.push_screen(DoomScreen())
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert doom_calls == ["w"], doom_calls
            assert app.chat_input.value == "", "chat history was browsed on the overlay"
            await pilot.press("down")
            await pilot.pause()
            assert doom_calls == ["w", "s"], doom_calls
            await pilot.press("space")
            await pilot.pause()
            assert doom_calls == ["w", "s", "shoot"], doom_calls
            # leaving the overlay restores history browsing
            app.pop_screen()
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            assert app.chat_input.value == "an old line"
            assert doom_calls == ["w", "s", "shoot"], doom_calls

    asyncio.run(check())


def test_doom_game_keys_live_on_the_overlay_not_the_app(headless_app):
    """Regression: arrows/space used to be app-level bindings that only
    worked because the controller re-checked the screen. They are
    DoomScreen bindings now; the app binds none of them."""

    app = headless_app
    keys = app._bindings.key_to_bindings
    for gone in ("up", "down", "left", "right", "space", "escape"):
        assert gone not in keys, f"app still binds {gone} (doom key leak)"
    doom_actions = [b.action for b in DoomScreen.BINDINGS]
    for move in ("press_key('w')", "press_key('s')", "press_key('a')", "press_key('d')", "press_key('shoot')"):
        assert move in doom_actions, f"DoomScreen lost {move}"


# -- regression: modal inputs must not leak into chat --------------------------


def test_modal_input_submitted_does_not_run_chat(headless_app, monkeypatch):
    """Enter in the rename prompt dismisses the prompt — it must not
    also clear the chat line, push history, and run route_chat_message with
    the modal text (Submitted bubbles up to the app)."""

    app = headless_app
    chats, commands = [], []
    monkeypatch.setattr(app, "route_chat_message", chats.append)
    monkeypatch.setattr(app, "dispatch_command", commands.append)

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


def test_tab_on_modal_does_not_focus_hidden_chat(headless_app):
    """Regression: Tab with a modal on top used to pull focus onto the
    main screen's (invisible) chat input whenever that input was not
    focused — e.g. the user clicked the transcript, then right-clicked an
    organism to open the rename prompt. Tab on a modal is the modal's
    business: focus must stay inside the modal."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            app.chat_input.focus()
            await pilot.pause()
            app.set_focus(None)  # the user clicked the transcript first
            await pilot.pause()
            assert not app.chat_input.has_focus
            app.push_screen(RenameScreen("default"))
            await pilot.pause()
            assert isinstance(app.screen, RenameScreen)
            prompt = app.screen.query_one("#rename-input", Input)
            assert app.focused is prompt
            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is prompt, "Tab leaked focus to the hidden chat input"
            assert app.chat_input.value == "", "Tab ran chat completion on the hidden input"
            assert app._completion_matches is None

    asyncio.run(check())


# -- regression: command palette -----------------------------------------------


def test_palette_enter_runs_no_arg_command(headless_app):
    """Enter in the palette filter box must run the first matching no-arg
    command (the palette input used to have no submit handler anywhere,
    making Enter a dead key)."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert isinstance(pilot.app.screen, CommandPalette)
            await pilot.press("slash", "s", "t", "a")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(pilot.app.screen, CommandPalette), "Enter did not close the palette"
            lines = [str(line.text) for line in app.query_one("#dreams", RichLog).lines]
            assert any("— awake · mood:" in line for line in lines), lines
            assert any(line.startswith("mind:") for line in lines), lines
            assert app.chat_input.value == "", "palette run left the command in the input"
            assert app.chat_input.has_focus

    asyncio.run(check())


def test_palette_enter_only_fills_input_for_arg_command(headless_app):
    """Enter on a command with placeholder args fills the chat line instead
    of running it — submitting '/chaos 0..1' literally would do nonsense."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            await pilot.press("ctrl+p")
            await pilot.pause()
            await pilot.press("slash", "c", "h", "a", "o")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(pilot.app.screen, CommandPalette)
            assert app.chat_input.value == "/chaos "
            assert app.chat_input.has_focus

    asyncio.run(check())


def test_palette_enter_with_no_matches_stays_open(headless_app):
    """Enter with an empty filter result must not dispatch anything or
    close the palette on a bogus pick."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            await pilot.press("ctrl+p")
            await pilot.pause()
            await pilot.press(*"xyzxyz")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(pilot.app.screen, CommandPalette), "palette closed on no-match Enter"
            assert app.chat_input.value == ""

    asyncio.run(check())


def test_palette_arrows_select_row_for_enter(headless_app):
    """↓ from the filter box highlights the next command row (skipping
    category headers); Enter runs THAT command, not the first filter
    match."""

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            await pilot.press("ctrl+p")
            await pilot.pause()
            # "cam" matches exactly two commands, in COMMANDS order:
            # /look ("grab a camera frame…") then /camera.
            await pilot.press("c", "a", "m")
            await pilot.pause()
            await pilot.press("down")  # /look
            await pilot.pause()
            await pilot.press("down")  # /camera
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.chat_input.value == "/camera ", app.chat_input.value
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
