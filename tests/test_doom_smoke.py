"""Headless smoke test: start doom-ascii via the real TUI command path and
verify the organism observes the frame, with the game rendering into the
DoomScreen overlay instead of a tab pane.

Hermetic: DOOM_ASCII_BIN points at the committed stub binary double, so no
C toolchain, network, or WAD is needed.
"""

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, Label, Static

from conftest import neuter_background_loops, wait_until
from replicanta import nursery as nursery_mod
from replicanta.organism import Organism
from replicanta.tui import DoomScreen, OrganismApp

STUB = Path(__file__).parent / "fixtures" / "doom_ascii_stub.py"


@pytest.fixture
def doom_app(monkeypatch, tmp_path):
    seed = tmp_path / "organism.scl"
    seed.write_text("type bel(x: String, a: String, v: String)\n")
    nursery_mod.create(tmp_path, "doomtest", seed)
    # Copy the real modules tree into the temp nursery root so load() finds them.
    import shutil

    shutil.copytree(Path(__file__).parent.parent / "modules", tmp_path / "modules")
    # Default modules list does not include doom-ascii unless config enables it.
    (tmp_path / "replicanta.toml").write_text('[modules]\nenabled = ["base", "doom-ascii"]\n')
    # Point the doom service at the stub binary double (hermetic, no WAD).
    wad = tmp_path / "doom1.wad"
    wad.write_bytes(b"PWAD fake")
    monkeypatch.setenv("DOOM_ASCII_BIN", str(STUB))
    monkeypatch.setenv("DOOM_WAD", str(wad))
    monkeypatch.setenv("DOOM_ASCII_ARGS", "--interval 0.02")
    org_dir = nursery_mod.organism_dir(tmp_path, "doomtest")
    org = Organism(org_dir)
    org.load()
    app = OrganismApp(org, root=tmp_path)
    neuter_background_loops(monkeypatch, app)
    monkeypatch.setattr(app, "refresh_status", lambda: None)
    yield app
    app.org.module_loader.registry.get("doom").stop()


async def _start_doom(app, pilot):
    """Drive '/doom start' through the chat line and wait for the overlay."""
    chat = app.query_one("#chat", Input)
    chat.focus()
    chat.value = "/doom start"
    await pilot.press("enter")
    await wait_until(
        lambda: type(app.screen) is DoomScreen,
        message="doom overlay to open",
    )
    return app.screen.query_one("#doom", Static)


def test_doom_command_renders_frame(doom_app):
    app = doom_app

    async def check():
        async with app.run_test() as pilot:
            doom = await _start_doom(app, pilot)

            def painted():
                content = doom._Static__content
                plain = getattr(content, "plain", str(content))
                return len(plain.splitlines()) > 5

            await wait_until(painted, message="doom frame to render")
            assert app.org.store.belief_value("doom", "frame") == "running"

    asyncio.run(check())


def test_doom_command_observes_frame(doom_app):
    app = doom_app

    async def check():
        async with app.run_test() as pilot:
            await _start_doom(app, pilot)

            def observed():
                memories = [m["text"] for m in app.org.store.memory if m.get("kind") == "doom"]
                return any("doom-ascii" in m for m in memories)

            await wait_until(observed, message="doom frame observation to be remembered")
            assert app.org.store.belief_value("doom", "frame") == "running"

    asyncio.run(check())


def test_doom_frame_renderable_prefers_ansi_styles():
    """Unit: the pane renderable must stay a styled Rich Text — a str()
    conversion anywhere in the controller flattens the truecolor styles
    into uncolored soup (the 'distorted raw ascii' regression). A parseable
    block frame becomes half-block cells; anything else falls back to
    Text.from_ansi."""
    from rich.text import Text

    from replicanta.tui_controllers import doom_frame_renderable

    class FakeSvc:
        def frame_ansi(self):
            return "\x1b[38;2;255;0;0m██\x1b[0m"

        def frame(self):
            return "plain"

    renderable = doom_frame_renderable(FakeSvc(), running=True)
    assert isinstance(renderable, Text)
    assert renderable.plain == "▀"  # red pixel over the black padding row
    assert any(s.style for s in renderable.spans)
    # no game -> nothing to paint
    assert doom_frame_renderable(FakeSvc(), running=False) == ""

    class UnparsableSvc:
        def frame_ansi(self):
            return "\x1b[0m"  # no pixels: half-block parse yields None

    fallback = doom_frame_renderable(UnparsableSvc(), running=True)
    assert isinstance(fallback, Text)  # Text.from_ansi fallback, no crash

    class PlainOnly:
        def frame(self):
            return "plain"

    # no ansi API -> plain string fallback
    assert doom_frame_renderable(PlainOnly(), running=True) == "plain"


def test_doom_pane_receives_colored_frame(doom_app):
    """End-to-end through the Lua module and the controller: the #doom
    Static must hold a Text whose spans carry the game's truecolor styles."""
    from rich.text import Text

    app = doom_app

    async def check():
        async with app.run_test() as pilot:
            doom = await _start_doom(app, pilot)

            def colored():
                content = doom._Static__content
                return isinstance(content, Text) and any(s.style for s in content.spans)

            await wait_until(colored, message="styled frame to reach the pane")
            # the pane shows half-block pixels now, not the raw stub text
            assert "█" in doom._Static__content.plain or "▀" in doom._Static__content.plain

    asyncio.run(check())


def test_manual_keypress_pauses_entity_autoplay(doom_app):
    """A human arrow key puts the entity's auto-play on cooldown and must
    not immediately re-arm it — previously every keypress scheduled an
    entity turn 300ms later, so the game played itself against the human."""
    import time

    app = doom_app

    async def check():
        async with app.run_test() as pilot:
            await _start_doom(app, pilot)
            await wait_until(
                lambda: app.org.module_loader.registry.get("doom").frame_count() > 0,
                message="stub frames to flow",
            )
            app._doom.key_command("w")
            assert app._doom._manual_until > time.monotonic()
            app._doom.schedule_turn()
            assert app._doom.pending_turns() == []  # cooldown: the entity may not move
            # once the cooldown lapses, auto-play may schedule again
            app._doom._manual_until = 0.0
            app._responding = False
            app._doom.schedule_turn()
            assert len(app._doom.pending_turns()) == 1

    asyncio.run(check())


def test_repaint_interval_runs_only_during_a_game(doom_app):
    """The overlay must repaint at game speed while a game runs (the 1s app
    tick makes manual play feel frozen) and stop when the game ends."""
    app = doom_app

    async def check():
        async with app.run_test() as pilot:
            await _start_doom(app, pilot)
            await wait_until(
                lambda: app._doom._repaint_timer is not None,
                message="repaint loop to start with the game",
            )
            app._doom.command(["stop"])
            assert app._doom._repaint_timer is None

    asyncio.run(check())


def test_overlay_arrows_reach_the_game_even_when_the_frame_overflows(doom_app):
    """Regression: when the frame is taller than the terminal (scaling 2),
    the ScrollableContainer shows a scrollbar and a FOCUSED one consumes
    up/down for scrolling — the player couldn't walk. The container must
    not take focus, so every game key reaches the bindings."""
    app = doom_app

    async def check():
        async with app.run_test(size=(90, 16)) as pilot:  # force overflow
            doom = await _start_doom(app, pilot)

            def painted():
                content = doom._Static__content
                plain = getattr(content, "plain", str(content))
                return len(plain.splitlines()) > 5

            await wait_until(painted, message="doom frame to render")
            scroll = app.screen.query_one("#doom-scroll")
            assert scroll.show_vertical_scrollbar
            got = []
            real = app._doom.key_command
            app._doom.key_command = lambda cmd: (got.append(cmd), real(cmd))[1]
            try:
                for key in ("up", "down", "left", "right", "space"):
                    await pilot.press(key)
                    await pilot.pause()
            finally:
                app._doom.key_command = real
            assert sorted(got) == ["a", "d", "s", "shoot", "w"], got

    asyncio.run(check())


def test_overlay_wasd_qe_bound_on_the_overlay_only(doom_app):
    """Regression: w/s/q/e did nothing on the overlay (only arrows/space had
    bindings, and app-level w/s would swallow chat typing) — the movement
    keys live on DoomScreen itself, and each press echoes into the hint."""
    app = doom_app

    async def check():
        async with app.run_test(size=(90, 16)) as pilot:
            await _start_doom(app, pilot)
            got = []
            real = app._doom.key_command
            app._doom.key_command = lambda cmd: (got.append(cmd), real(cmd))[1]
            try:
                for key in ("w", "s", "a", "d", "q", "e"):
                    await pilot.press(key)
                    await pilot.pause()
            finally:
                app._doom.key_command = real
            assert sorted(got) == ["a", "d", "e", "q", "s", "w"], got
            hint = app.screen.query_one("#doom-hint", Label)
            assert "you: strafe right" in str(hint._Static__content)

    asyncio.run(check())


def test_main_screen_typing_w_stays_in_chat(doom_app):
    """Guard: the overlay's w/s bindings must not leak to the main screen —
    typing a sentence with w in the chat line must reach the Input, not the
    doom game."""
    app = doom_app

    async def check():
        async with app.run_test(size=(90, 16)) as pilot:
            chat = app.query_one("#chat", Input)
            chat.focus()
            await pilot.press("w")
            await pilot.pause()
            assert chat.value == "w"
            assert app._doom._manual_until == 0.0  # no game, no cooldown armed

    asyncio.run(check())


def test_overlay_chat_input_roundtrip(doom_app):
    """The overlay must show its own chat line (the main input is hidden
    beneath the full-screen overlay). Game keys play by default; Tab enters
    typing mode (letters reach the Input, not the game); Enter routes the
    line as a move, echoes it in the thought stream, and returns focus to
    the game."""
    app = doom_app

    async def check():
        async with app.run_test(size=(90, 24)) as pilot:
            await _start_doom(app, pilot)
            svc = app.org.module_loader.registry.get("doom")
            await wait_until(lambda: svc.frame_count() > 0, message="stub frames to flow")
            # Arm the manual cooldown first: auto-play would otherwise keep
            # taking turns, and its thought stream ages the "you › w" echo
            # out of the trimmed 8-line window before the final assertion.
            await pilot.press("s")
            await pilot.pause()
            overlay_input = app.screen.query_one("#doom-chat", Input)
            # the game keys own the keyboard until Tab: no auto-focus
            assert app.focused is not overlay_input
            got = []
            real_key = app._doom.key_command
            app._doom.key_command = lambda cmd: (got.append(cmd), real_key(cmd))[1]
            real_cmd = app._doom.command
            sent = []
            app._doom.command = lambda args: (sent.append(list(args)), real_cmd(args))[1]
            try:
                await pilot.press("tab")
                await pilot.pause()
                assert app.focused is overlay_input  # typing mode
                await pilot.press("w")
                await pilot.pause()
                assert overlay_input.value == "w"  # typed, not played
                assert got == []
                await pilot.press("enter")
                await pilot.pause()
                assert ["w"] in sent  # submitted line moved the player
                assert overlay_input.value == ""
                assert app.focused is not overlay_input  # back to game keys
            finally:
                app._doom.key_command = real_key
                app._doom.command = real_cmd
            thoughts = app.screen.query_one("#doom-thoughts", Static)
            assert "you › w" in str(thoughts._Static__content)

    asyncio.run(check())


def test_typed_movement_words_reach_the_game(doom_app):
    """Typing 'w' in chat during a game must move the player (the module
    contract promises movement lines); single-letter moves were rejected
    and fell through to the entity as conversation."""
    app = doom_app

    async def check():
        async with app.run_test() as pilot:
            await _start_doom(app, pilot)
            svc = app.org.module_loader.registry.get("doom")
            await wait_until(lambda: svc.frame_count() > 0, message="stub frames to flow")
            assert app._doom.chat_command("w") is True
            await wait_until(lambda: "key=up" in svc.frame(), message="typed 'w' to move the player")

    asyncio.run(check())


def test_doom_stop_halts_auto_play(doom_app, monkeypatch):
    """Regression: /doom stop must kill the game process and silence the
    auto-play loop — no pending turn timers, no new game re-armed."""
    from replicanta import voice

    app = doom_app
    monkeypatch.setattr(voice, "doom_move", lambda org, **kw: 'moving.\ndoom.command("w")')
    monkeypatch.setattr(voice, "online", lambda: True)

    async def check():
        async with app.run_test() as pilot:
            doom = await _start_doom(app, pilot)

            def painted():
                content = doom._Static__content
                plain = getattr(content, "plain", str(content))
                return len(plain.splitlines()) > 5

            await wait_until(painted, message="doom frame to render")
            await asyncio.sleep(1.0)  # let auto-play take a few turns

            # esc closes the overlay; the game keeps running until /doom stop
            await pilot.press("escape")
            await pilot.pause()
            assert type(app.screen) is not DoomScreen
            assert app.org.module_loader.registry.get("doom").running()

            chat = app.query_one("#chat", Input)
            chat.focus()
            chat.value = "/doom stop"
            await pilot.press("enter")
            await asyncio.sleep(0.5)

            svc = app.org.module_loader.registry.get("doom")
            assert svc.running() is False
            assert app._doom.pending_turns() == []  # the loop is not re-armed
            await asyncio.sleep(1.5)
            assert svc.running() is False  # no new game booted

    asyncio.run(check())


def test_cancel_auto_stops_pending_turns(doom_app, monkeypatch):
    """Regression: cancel_auto must actually cancel. Bound methods never
    match with `is`, so manual input used to leave the pending entity turn
    alive and the entity grabbed the keyboard back 300ms later."""
    from replicanta import voice

    app = doom_app
    monkeypatch.setattr(voice, "online", lambda: True)

    async def check():
        async with app.run_test() as pilot:
            await _start_doom(app, pilot)
            await wait_until(
                lambda: app.org.module_loader.registry.get("doom").frame_count() > 0,
                message="stub frames to flow",
            )
            app._doom._manual_until = 0.0
            app._responding = False
            app._doom.schedule_turn()
            assert len(app._doom.pending_turns()) >= 1  # a turn is queued
            app._doom.cancel_auto()
            assert app._doom.pending_turns() == []  # and now it is not

    asyncio.run(check())


# -- org-identity binding: controllers must not outlive their organism --------


class _FakeTimer:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _SwapRegistry:
    def __init__(self, services):
        self._services = services

    def get(self, name):
        return self._services.get(name)


class _SwapLoader:
    def __init__(self, registry):
        self.registry = registry


class _FakeDoomSvc:
    """Module-shaped doom double: start/stop flip running() like the real
    Lua service does through commands.dispatch."""

    def __init__(self):
        self._running = False

    def running(self):
        return self._running

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    def status(self):
        return "doom-ascii: skill 1 · frame 1 · running — /doom stop ends it"

    def set_viewport(self, _cols):
        pass

    def yield_to_human(self, _secs):
        pass

    def frame_ansi(self):
        return ""

    def frame(self):
        return ""


class _FakeCommands:
    def __init__(self, svc):
        self._svc = svc

    def dispatch(self, _name, args):
        if args and args[0] == "start":
            self._svc.start()
            return "started"
        if args and args[0] == "stop":
            self._svc.stop()
            return "stopped"
        return self._svc.status()


class _SwapApp:
    """Duck-typed OrganismApp: just enough surface for the controllers."""

    def __init__(self, org):
        self.org = org
        self._responding = False
        self._self_talking = False
        self.log = []
        self.pushed = []

    def set_timer(self, _delay, _cb, **kw):
        return _FakeTimer()

    def set_interval(self, _interval, _cb, **kw):
        return _FakeTimer()

    def call_from_thread(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def _append_log(self, text, *args, **kwargs):
        self.log.append(text)

    def refresh_status(self):
        pass

    def _safe_query(self, *args, **kwargs):
        return None

    def push_screen(self, screen):
        self.pushed.append(screen)

    @property
    def screen(self):
        return None  # no DoomScreen/MudScreen overlay in the harness


def _swap_organism(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "organism.scl").write_text("type bel(x: String, a: String, v: String)\n")
    org = Organism(tmp_path)
    org.load()
    return org


def _seat_doom(org):
    svc = _FakeDoomSvc()
    org.module_loader = _SwapLoader(_SwapRegistry({"doom": svc, "commands": _FakeCommands(svc)}))
    return svc


def test_doom_controller_drops_session_on_organism_swap(tmp_path, monkeypatch):
    """Regression: DoomController reached self._app.org dynamically, so
    after an organism swap the auto-play loop kept running and painted the
    NEW organism's game. The controller binds the organism that started the
    session and must no-op (stop repaint, cancel turns) on a swap."""
    from replicanta.tui_controllers import DoomController

    org1 = _swap_organism(tmp_path / "one")
    svc1 = _seat_doom(org1)
    app = _SwapApp(org1)
    doom = DoomController(app)

    doom.command(["start"])
    assert svc1.running()
    assert doom._org is org1
    assert org1.store.belief_value("doom", "frame") == "running"

    # swap: a new organism whose own game is ALREADY running
    org2 = _swap_organism(tmp_path / "two")
    svc2 = _seat_doom(org2)
    svc2.start()
    app.org = org2

    doom._repaint_tick()  # heartbeat after the swap
    assert doom._org is None  # stale binding cleaned up
    assert doom._repaint_timer is None
    assert doom.pending_turns() == []
    assert svc2.running()  # the new organism's game is untouched

    # a queued turn landing after the swap must not dispatch a worker
    app._responding = False
    doom.take_turn()
    assert app._responding is False  # refused: the session belongs to org1

    # nothing the controller did landed in the new organism's store
    assert org2.store.belief_value("doom", "frame") is None
    assert not any(m.get("kind") == "doom" for m in org2.store.memory)
    # ...and the old organism keeps its own observations
    assert org1.store.belief_value("doom", "frame") == "running"

    # reset_for_swap is the public hook swap flows call
    org3 = _swap_organism(tmp_path / "three")
    _seat_doom(org3)
    app.org = org3
    doom.reset_for_swap()
    assert doom._org is None
    assert doom._manual_until == 0.0


def test_mud_controller_drops_session_without_saving_on_swap(tmp_path, monkeypatch):
    """Regression: MudController kept auto-playing after a swap and saved
    the OLD game's session into the NEW organism's store. The session must
    be dropped WITHOUT saving the moment a heartbeat notices the swap."""
    from replicanta import mud as mud_mod
    from replicanta.tui_controllers import MudController

    org1 = _swap_organism(tmp_path / "one")
    app = _SwapApp(org1)
    controller = MudController(app)
    controller.start(scenario=mud_mod.default_scenario(), fresh=True)
    assert controller.game is not None
    assert controller._org is org1
    assert org1.store.load_mud_session() is not None  # saved at start

    org2 = _swap_organism(tmp_path / "two")
    app.org = org2
    controller.paused = False
    controller.next_turn()  # the turn heartbeat fires after the swap
    assert controller.game is None  # session dropped, not applied
    assert org2.store.load_mud_session() is None  # nothing persisted
    assert not any(m.get("kind") == "mud" for m in org2.store.memory)
    # the old organism's saved session is intact
    assert org1.store.load_mud_session() is not None

    # route_text must not consume or hint for a stale session
    app.org = org2
    controller.game = None
    controller._org = None
    assert controller.route_text("go north") is False


def _pane_plain(static):
    content = static._Static__content
    return getattr(content, "plain", str(content))


async def _start_doom_inline(app, pilot):
    """Drive '/doom start' on a wide terminal and wait for the inline pane
    (no fullscreen overlay)."""
    chat = app.query_one("#chat", Input)
    chat.focus()
    chat.value = "/doom start"
    await pilot.press("enter")
    await wait_until(lambda: app._doom_pane_visible, message="inline doom pane to appear")
    return app.query_one("#doom-pane-frame", Static)


def test_doom_start_uses_inline_pane_on_wide_terminals(doom_app):
    """Wide terminals: the game renders in a right-hand pane — the chat
    log, sidebar, and chat input stay live and the chat input keeps focus.
    No fullscreen takeover, no esc-to-return."""
    app = doom_app

    async def check():
        async with app.run_test(size=(160, 40)) as pilot:
            chat = app.query_one("#chat", Input)
            chat.focus()
            chat.value = "/doom start"
            await pilot.press("enter")
            await wait_until(lambda: app._doom_pane_visible, message="inline pane to appear")
            assert type(app.screen) is not DoomScreen  # main screen stays up
            assert app.focused is chat  # typing remains the default mode
            frame = app.query_one("#doom-pane-frame", Static)
            await wait_until(
                lambda: len(_pane_plain(frame).splitlines()) > 5,
                message="doom frame to render into the pane",
            )
            assert app._doom.game_running()

    asyncio.run(check())


def test_doom_pane_tab_cycles_typing_and_playing(doom_app):
    """Tab on the main screen cycles typing ⇄ playing, the same convention
    as the overlay's chat line: playing mode routes game keys to the game,
    typing mode returns them to the chat input."""
    app = doom_app

    async def check():
        async with app.run_test(size=(160, 40)) as pilot:
            await _start_doom_inline(app, pilot)
            chat = app.query_one("#chat", Input)
            got = []
            real = app._doom.pane_key_command
            app._doom.pane_key_command = lambda cmd: (got.append(cmd), real(cmd))[1]
            try:
                await pilot.press("tab")
                await pilot.pause()
                assert app.focused is not chat  # playing mode
                await pilot.press("w")
                await pilot.pause()
                assert got == ["w"]  # played, not typed
                assert chat.value == ""
                await pilot.press("tab")
                await pilot.pause()
                assert app.focused is chat  # back to typing
                await pilot.press("w")
                await pilot.pause()
                assert chat.value == "w"  # typed, not played
                assert got == ["w"]
            finally:
                app._doom.pane_key_command = real

    asyncio.run(check())


def test_doom_view_always_opens_the_fullscreen_overlay(doom_app):
    """`/doom view` opens the fullscreen overlay even on wide terminals —
    the pane is the default, the overlay stays available on demand."""
    app = doom_app

    async def check():
        async with app.run_test(size=(160, 40)) as pilot:
            chat = app.query_one("#chat", Input)
            chat.focus()
            chat.value = "/doom view"
            await pilot.press("enter")
            await wait_until(
                lambda: type(app.screen) is DoomScreen,
                message="fullscreen overlay to open",
            )
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1

    asyncio.run(check())
