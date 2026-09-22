"""Headless smoke test: start doom-ascii via the real TUI command path and
verify the organism observes the frame.

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
from replicanta.tui import OrganismApp

STUB = Path(__file__).parent / "fixtures" / "doom_ascii_stub.py"


@pytest.fixture
def doom_app(monkeypatch, tmp_path):
    seed = tmp_path / "organism.scl"
    seed.write_text("type bel(x: String, a: String, v: String)\n")
    nursery_mod.create(tmp_path, "doomtest", seed)
    # Copy the real modules tree into the temp nursery root so load() finds them.
    import shutil

    shutil.copytree(Path(__file__).parent.parent / "modules", tmp_path / "modules")
    # Default modules list does not include nano-doom unless config enables it.
    (tmp_path / "replicanta.toml").write_text('[modules]\nenabled = ["base", "nano-doom"]\n')
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


def test_doom_command_renders_frame(doom_app):
    app = doom_app

    async def check():
        async with app.run_test() as pilot:
            chat = app.query_one("#chat", Input)
            chat.focus()
            chat.value = "/doom start"
            await pilot.press("enter")
            doom = app.query_one("#doom", Static)
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
            chat = app.query_one("#chat", Input)
            chat.focus()
            chat.value = "/doom start"
            await pilot.press("enter")

            def observed():
                memories = [m["text"] for m in app.org.store.memory if m.get("kind") == "doom"]
                return any("doom-ascii" in m for m in memories)

            await wait_until(observed, message="doom frame observation to be remembered")
            assert app.org.store.belief_value("doom", "frame") == "running"

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
            chat = app.query_one("#chat", Input)
            chat.focus()
            chat.value = "/doom start"
            await pilot.press("enter")
            doom = app.query_one("#doom", Static)
            await wait_until(
                lambda: "DOOM-ASCII STUB" in str(doom.render()),
                message="doom frame to render",
            )
            await asyncio.sleep(1.0)  # let auto-play take a few turns

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
