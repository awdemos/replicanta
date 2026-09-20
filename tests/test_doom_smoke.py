"""Headless smoke test: start nano-doom via the real TUI command path and
verify the organism observes the frame."""

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Input, Static

from replicanta import nursery as nursery_mod
from replicanta.organism import Organism
from replicanta.tui import OrganismApp


@pytest.fixture
def headless_app(monkeypatch, tmp_path):
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
    monkeypatch.setattr(app, "_probe_voice", lambda: None)
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
    monkeypatch.setattr(app, "_on_tick", lambda: None)
    monkeypatch.setattr(app, "refresh_status", lambda: None)
    return app


def test_doom_command_renders_frame(headless_app):
    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            chat = app.query_one("#chat", Input)
            chat.focus()
            chat.value = "/doom start"
            await pilot.press("enter")
            await asyncio.sleep(1.0)
            # The frame now renders in the dedicated DOOM pane, not the chat log.
            doom = app.query_one("#doom", Static)
            text = str(doom.render())
            assert "hp=" in text
            assert app.org.store.belief_value("doom", "frame") == "running"

    asyncio.run(check())


def test_doom_command_observes_frame(headless_app):
    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            chat = app.query_one("#chat", Input)
            chat.focus()
            chat.value = "/doom start"
            await pilot.press("enter")
            await asyncio.sleep(0.5)
            memories = [m["text"] for m in app.org.store.memory if m.get("kind") == "doom"]
            assert any("hp=" in m for m in memories)
            assert app.org.store.belief_value("doom", "frame") == "running"

    asyncio.run(check())
