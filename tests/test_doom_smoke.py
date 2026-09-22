"""Headless smoke test: start doom-ascii via the real TUI command path and
verify the organism observes the frame, with the game rendering into the
DoomScreen overlay instead of a tab pane.

Hermetic: DOOM_ASCII_BIN points at the committed stub binary double, so no
C toolchain, network, or WAD is needed.
"""

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, Static

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
            armed = []
            real_set_timer = app.set_timer

            def spy(delay, callback=None, **kw):
                if callback == app._doom.take_turn:
                    armed.append(callback)
                return real_set_timer(delay, callback, **kw)

            app.set_timer = spy
            try:
                app._doom.key_command("w")
                assert app._doom._manual_until > time.monotonic()
                app._doom.schedule_turn()
                assert armed == []  # cooldown: the entity may not move
                # once the cooldown lapses, auto-play may schedule again
                app._doom._manual_until = 0.0
                app._responding = False
                app._doom.schedule_turn()
                assert len(armed) == 1
            finally:
                app.set_timer = real_set_timer

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
            assert app._doom._turn_timers() == []  # the loop is not re-armed
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
            assert len(app._doom._turn_timers()) >= 1  # a turn is queued
            app._doom.cancel_auto()
            assert app._doom._turn_timers() == []  # and now it is not

    asyncio.run(check())
