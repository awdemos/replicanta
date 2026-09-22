"""Headless smoke test: start nano-doom via the real TUI command path and
verify the organism observes the frame."""

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, Static

from conftest import neuter_background_loops, wait_until
from replicanta import nursery as nursery_mod
from replicanta.organism import Organism
from replicanta.tui import OrganismApp


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
    org_dir = nursery_mod.organism_dir(tmp_path, "doomtest")
    org = Organism(org_dir)
    org.load()
    app = OrganismApp(org, root=tmp_path)
    neuter_background_loops(monkeypatch, app)
    monkeypatch.setattr(app, "refresh_status", lambda: None)
    return app


def test_doom_command_renders_frame(doom_app):
    app = doom_app

    async def check():
        async with app.run_test() as pilot:
            chat = app.query_one("#chat", Input)
            chat.focus()
            chat.value = "/doom start"
            await pilot.press("enter")
            # The doom loop's first frame is genuinely timer-driven, so
            # poll bounded rather than fixed-sleeping.
            doom = app.query_one("#doom", Static)
            await wait_until(lambda: "hp=" in str(doom.render()), message="doom frame to render")
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
                return any("hp=" in m for m in memories)

            await wait_until(observed, message="doom frame observation to be remembered")
            assert app.org.store.belief_value("doom", "frame") == "running"

    asyncio.run(check())


def test_doom_stop_halts_auto_play(doom_app, monkeypatch):
    """Regression: /doom stop must halt the game and silence the auto-play
    loop — no pending turn timers, no further advances."""
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
            await wait_until(lambda: "hp=" in str(doom.render()), message="doom frame to render")
            await asyncio.sleep(1.5)  # let auto-play advance a few turns

            chat.value = "/doom stop"
            await pilot.press("enter")
            await asyncio.sleep(0.5)

            svc = app.org.module_loader.registry.get("doom")
            assert svc.running() is False
            pending = [t for t in getattr(app, "_timers", []) if getattr(t, "_callback", None) is app._doom.take_turn]
            assert pending == []  # the loop is not re-armed
            turns_at_stop = re_turns(svc)
            await asyncio.sleep(1.5)
            assert re_turns(svc) == turns_at_stop  # nothing advances anymore

    asyncio.run(check())


def re_turns(svc):
    import re

    match = re.search(r"turns=(\d+)", svc.status())
    return match.group(1) if match else None
