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
            await wait_until(
                lambda: "DOOM-ASCII STUB" in str(doom.render()),
                message="doom frame to render",
            )
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
    into uncolored soup (the 'distorted raw ascii' regression)."""
    from rich.text import Text

    from replicanta.tui_controllers import doom_frame_renderable

    class FakeSvc:
        def frame_ansi(self):
            return "\x1b[38;2;255;0;0mAB\x1b[0m"

        def frame(self):
            return "plain"

    renderable = doom_frame_renderable(FakeSvc(), running=True)
    assert isinstance(renderable, Text)
    assert renderable.plain == "AB"
    assert any(s.style for s in renderable.spans)
    # no game -> nothing to paint
    assert doom_frame_renderable(FakeSvc(), running=False) == ""

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
            assert "DOOM-ASCII STUB" in doom._Static__content.plain

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
            await wait_until(
                lambda: "DOOM-ASCII STUB" in str(doom.render()),
                message="doom frame to render",
            )
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
            pending = [t for t in getattr(app, "_timers", []) if getattr(t, "_callback", None) is app._doom.take_turn]
            assert pending == []  # the loop is not re-armed
            await asyncio.sleep(1.5)
            assert svc.running() is False  # no new game booted

    asyncio.run(check())
