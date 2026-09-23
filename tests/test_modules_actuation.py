"""Entity-initiated actuation locks for the doom-ascii and fly-brain modules.

Two security contracts, tested at module level through the real Lua
modules:

1. Process creation must not be reachable from free model text: a reply
   containing ``doom.start(...)`` must never spawn a game (games begin via
   /doom, the arrow keys, or the controller's explicit start path). The
   entity can still PLAY a user-started game (whitelisted moves).
2. With the organism's ``entity_actuation`` flag off, entity-issued doom
   moves and brain runs/banks from the utterance hook must not execute —
   they log one line and skip. User-initiated paths (/doom, /brain) are
   never gated.
"""

import shutil
import time
from pathlib import Path

import pytest

from replicanta.modules import ModuleLoader

MODULES_SRC = Path(__file__).parent.parent / "modules"
STUB = Path(__file__).parent / "fixtures" / "doom_ascii_stub.py"


def _wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def doom_loader(tmp_path, monkeypatch):
    wad = tmp_path / "doom1.wad"
    wad.write_bytes(b"PWAD fake")
    monkeypatch.setenv("DOOM_ASCII_BIN", str(STUB))
    monkeypatch.setenv("DOOM_WAD", str(wad))
    monkeypatch.setenv("DOOM_ASCII_ARGS", "--interval 0.02")
    target = tmp_path / "modules"
    shutil.copytree(MODULES_SRC, target)
    loader = ModuleLoader(target, organism=None, modules_config={"enabled": ["base", "doom-ascii"]})
    loader.load_all()
    yield loader
    loader.registry.shutdown()


class _OrgFacade:
    """Stand-in for the organism facade with a fixed actuation flag."""

    def __init__(self, enabled):
        self._enabled = enabled

    def name(self):
        return "testorg"

    def entity_actuation(self):
        return self._enabled


def test_reply_with_doom_start_does_not_spawn_a_game(doom_loader, tmp_path, monkeypatch):
    """The blast-radius hole: model prose containing doom.start(N) used to
    spawn a process. Process creation from free text must be impossible;
    only explicit user/controller start paths begin a game."""
    loader = doom_loader
    hooks = loader.registry.get("hooks")
    doom = loader.registry.get("doom")
    hooks.emit("utterance", "doom.start(2)")
    hooks.emit("utterance", "ok, let me begin: doom.start()")
    hooks.emit("utterance", 'I will now run doom.start("3") to play')
    time.sleep(0.5)
    assert doom.running() is False
    assert "no game running" in doom.status() or "game over" in doom.status()


def test_entity_cannot_spawn_via_doom_command_start(doom_loader):
    """doom.command("start") routes to api.command's start branch — also a
    process creation from prose. The hook must filter it."""
    loader = doom_loader
    hooks = loader.registry.get("hooks")
    doom = loader.registry.get("doom")
    hooks.emit("utterance", 'doom.command("start")')
    time.sleep(0.5)
    assert doom.running() is False


def test_entity_still_plays_a_user_started_game(doom_loader):
    """The entity keeps its moves on a game the USER started (through the
    explicit /doom path), including buried-in-prose command lines."""
    loader = doom_loader
    hooks = loader.registry.get("hooks")
    commands = loader.registry.get("commands")
    doom = loader.registry.get("doom")
    commands.dispatch("/doom", ["start"])
    assert _wait_for(lambda: doom.frame_count() > 0)
    hooks.emit("utterance", 'The command is: doom.command("shoot").')
    assert _wait_for(lambda: "HIT!" in doom.frame())
    hooks.emit("utterance", "doom.command(w)")  # quote-less tolerance
    assert _wait_for(lambda: "key=up" in doom.frame())
    doom.stop()


def test_actuation_off_blocks_entity_doom_moves(doom_loader):
    """With entity_actuation False, an entity-issued doom.command does not
    move the player (user-started game, entity play gated)."""
    loader = doom_loader

    class _Off:
        def entity_actuation(self):
            return False

    loader.registry.register("organism", _Off())
    hooks = loader.registry.get("hooks")
    commands = loader.registry.get("commands")
    doom = loader.registry.get("doom")
    commands.dispatch("/doom", ["start"])
    assert _wait_for(lambda: doom.frame_count() > 0)
    hooks.emit("utterance", 'doom.command("d")')
    time.sleep(0.5)
    assert "key=right" not in doom.frame()  # the player never turned
    doom.stop()


def test_actuation_off_blocks_entity_brain_runs(tmp_path, monkeypatch):
    """fly-brain run/adapt/bank from the utterance hook are entity-initiated
    actuation: gated off, they log one line and skip."""
    from replicanta.organism import Organism

    org_dir = tmp_path / "org"
    org_dir.mkdir(parents=True)
    (org_dir / "organism.scl").write_text("type bel(x: String, a: String, v: String)\n")
    org = Organism(org_dir)
    org.load()
    org.entity_actuation = False

    fake = tmp_path / "wetware"
    fake.write_text(
        '#!/usr/bin/env python3\nimport json\nprint(json.dumps({"task": "digits", "test": 0.975, "bank_entries": 2}))\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("WETWARE_BIN", str(fake))
    target = tmp_path / "modules"
    shutil.copytree(MODULES_SRC, target)
    loader = ModuleLoader(
        target,
        organism=org,
        modules_config={"enabled": ["base", "fly-brain"]},
        emit=lambda _m: None,
    )
    loader.load_all()
    brain = loader.registry.get("brain")
    hooks = loader.registry.get("hooks")

    hooks.emit("utterance", 'brain.run("digits")')
    time.sleep(0.5)
    assert brain.running() is False
    assert brain.last() is None
    assert not any(m.get("kind") == "flybrain" for m in org.store.memory)

    # the user path is NOT gated: /brain run executes even with the flag off
    commands = loader.registry.get("commands")
    out = commands.dispatch("/brain", ["run", "digits"])
    assert "running" in out
    assert _wait_for(lambda: brain.last() is not None or not brain.running(), timeout=6.0)
    loader.registry.shutdown()
