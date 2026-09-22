"""Module-level tests for the pure-Lua doom-ascii capability.

The module spawns the committed stub binary double through ctx.process
(pty recipe) and parses frames in Lua — no Python service involved.
"""

import time
from pathlib import Path

import pytest

from replicanta.modules import ModuleLoader

STUB = str(Path(__file__).parent / "fixtures" / "doom_ascii_stub.py")
MODULES_SRC = Path(__file__).parent.parent / "modules"


def _wait_for(predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _load_module(tmp_path, monkeypatch, enabled, args="--interval 0.02"):
    import shutil

    wad = tmp_path / "doom1.wad"
    if not wad.exists():
        wad.write_bytes(b"PWAD fake")
    monkeypatch.setenv("DOOM_ASCII_BIN", STUB)
    monkeypatch.setenv("DOOM_WAD", str(wad))
    monkeypatch.setenv("DOOM_ASCII_ARGS", args)
    target = tmp_path / "modules"
    shutil.copytree(MODULES_SRC, target)
    loader = ModuleLoader(target, organism=None, modules_config={"enabled": enabled}, emit=lambda _m: None)
    loader.load_all()
    return loader


# -- plugin loading -----------------------------------------------------------------


def test_doom_service_absent_without_module(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base"])
    assert loader.registry.get("doom") is None


def test_doom_module_loads_and_registers(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    assert "doom-ascii" in loader.modules
    assert loader.registry.get("doom") is not None


def test_module_config_alias_nano_doom(tmp_path, monkeypatch):
    # Existing organism configs still say "nano-doom"; the loader maps it.
    loader = _load_module(tmp_path, monkeypatch, ["base", "nano-doom"])
    assert "doom-ascii" in loader.modules


# -- lifecycle ---------------------------------------------------------------------


def test_start_streams_frames_and_status(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    assert doom.start(3) is True
    assert _wait_for(lambda: "DOOM-ASCII STUB" in doom.frame())
    assert doom.running() is True
    assert "skill 3" in doom.status()
    assert doom.frame_count() > 0
    doom.stop()


def test_start_coerces_garbage_skill(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    doom.start("default")  # old nano-doom callers passed a map name
    assert _wait_for(lambda: "skill 1" in doom.status())
    doom.stop()


def test_status_without_game(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    assert "no game running" in loader.registry.get("doom").status()


def test_stop_halts_game(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    doom.start()
    assert _wait_for(lambda: doom.frame_count() > 0)
    assert doom.stop() is True
    assert doom.running() is False
    assert "no game running" in doom.status()


def test_game_exit_detected(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"], args="--frames 5 --interval 0.02")
    doom = loader.registry.get("doom")
    doom.start()
    assert _wait_for(lambda: doom.running() is False)
    assert "game over" in doom.status()


def test_start_while_running_replaces_game(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    doom.start()
    assert _wait_for(lambda: doom.frame_count() > 0)
    count = doom.frame_count()
    doom.start()  # replaces: frames reset, game runs again
    assert doom.running() is True
    assert doom.frame_count() < count + 5
    doom.stop()


def test_start_failure_surfaces_game_error_and_reaps(tmp_path, monkeypatch):
    """A game that cannot start fails loudly with its own stderr and is
    reaped by the cycle watchdog — never a silent 'starting' game."""
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"], args="--die")
    hooks = loader.registry.get("hooks")
    doom = loader.registry.get("doom")
    assert doom.start(1, 0.5) is True
    time.sleep(0.8)
    for _ in range(100):
        hooks.emit("cycle", "")
        if "IWAD or PWAD" in doom.status():
            break
        time.sleep(0.05)
    assert "IWAD or PWAD" in doom.status()
    assert doom.running() is False


# -- commands ------------------------------------------------------------------------


def test_command_shoot_marks_hit(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    doom.start()
    assert _wait_for(lambda: doom.frame_count() > 0)
    doom.command("shoot")
    assert _wait_for(lambda: "HIT!" in doom.frame())
    doom.stop()


def test_command_use_marks_open(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    doom.start()
    assert _wait_for(lambda: doom.frame_count() > 0)
    doom.command("use")
    assert _wait_for(lambda: "OPEN!" in doom.frame())
    doom.stop()


def test_command_movement_reaches_game(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    doom.start()
    assert _wait_for(lambda: doom.frame_count() > 0)
    doom.command("w")
    assert _wait_for(lambda: "key=up" in doom.frame())
    doom.stop()


def test_command_unknown_raises_lua_catchable(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    doom.start()
    assert _wait_for(lambda: doom.frame_count() > 0)
    with pytest.raises(Exception, match="unknown doom command"):
        doom.command("fly")
    doom.stop()


def test_command_without_game_raises(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    with pytest.raises(Exception, match="no game running"):
        loader.registry.get("doom").command("w")


# -- slash command and events --------------------------------------------------------


def test_module_slash_command_status_and_start(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    commands = loader.registry.get("commands")
    assert "no game running" in commands.dispatch("/doom", [])
    result = commands.dispatch("/doom", ["start"])
    assert "doom-ascii" in result
    doom = loader.registry.get("doom")
    assert _wait_for(lambda: doom.running())
    doom.stop()


def test_module_slash_command_direct_move(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    commands = loader.registry.get("commands")
    doom = loader.registry.get("doom")
    commands.dispatch("/doom", ["start"])
    assert _wait_for(lambda: doom.frame_count() > 0)
    commands.dispatch("/doom", ["w"])
    assert _wait_for(lambda: "key=up" in doom.frame())
    doom.stop()


def test_module_frame_api(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    commands = loader.registry.get("commands")
    doom = loader.registry.get("doom")
    commands.dispatch("/doom", ["start"])
    assert _wait_for(lambda: "DOOM-ASCII STUB" in doom.frame())
    assert "DOOM-ASCII STUB" in doom.frame_ansi()
    assert doom.frame_count() > 0
    doom.stop()


def test_frame_keeps_all_rows_including_status_line(tmp_path, monkeypatch):
    """The whole 25-row x 80-col frame must reach consumers: the bottom rows
    carry the status bar, and a truncated frame both hides them and, at the
    TUI pane's 78-column content width, wraps every line — shredding the
    picture into distorted raw ascii."""
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    doom.start()
    assert _wait_for(lambda: doom.frame_count() > 0)
    frame = doom.frame()
    lines = frame.splitlines()
    assert len(lines) == 25, f"frame lost rows: {len(lines)}"
    assert all(len(line) == 80 for line in lines)
    assert lines[0] == "#" * 80  # top border
    assert lines[-1] == "#" * 80  # bottom border / status row survives
    doom.stop()


def test_start_warps_straight_into_a_map(tmp_path, monkeypatch):
    """Regression: the game must autostart E1M1 (-warp 1 1). Without it the
    binary sits on the title screen and demo playback, where keys don't
    control anything — they only skip demos, so it was never playable."""
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    doom.start()
    assert _wait_for(lambda: "-warp 1 1" in doom.frame())
    doom.stop()


def test_start_uses_block_chars_and_playable_flags(tmp_path, monkeypatch):
    """The human-playable charset must reach the spawned argv: gradient
    letters are unreadable soup at game speed, so the module forces
    -chars block -nograd -fixgamma."""
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    doom.start()
    assert _wait_for(lambda: "chars=block" in doom.frame())
    assert "nograd=True" in doom.frame()
    assert "fixgamma=True" in doom.frame()
    doom.stop()


def test_scaling_follows_viewport_width(tmp_path, monkeypatch):
    """A wide terminal (>=166 cols) gets scaling 4 (160-col detail, the
    engine default); a narrow one gets scaling 8 (80-col fit)."""
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    doom = loader.registry.get("doom")
    doom.set_viewport(200)
    doom.start()
    assert _wait_for(lambda: "scaling=4" in doom.frame())
    doom.stop()
    doom.set_viewport(100)
    doom.start()
    assert _wait_for(lambda: "scaling=8" in doom.frame())
    doom.stop()


def test_entity_command_yields_to_the_human(tmp_path, monkeypatch):
    """Regression: while the human is playing, entity-issued commands must
    not execute. Every organism utterance fires the module's utterance
    hook, which used to run doom.command lines even mid-play."""
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    hooks = loader.registry.get("hooks")
    doom = loader.registry.get("doom")
    doom.start()
    assert _wait_for(lambda: doom.frame_count() > 0)
    hooks.emit("utterance", 'doom.command("shoot")')
    assert _wait_for(lambda: "HIT!" in doom.frame())
    doom.yield_to_human(30)
    hooks.emit("utterance", 'doom.command("w")')
    time.sleep(0.3)
    assert "key=up" not in doom.frame()  # the human holds the keyboard
    assert not doom.entity_command("w")  # lupa returns the (false, msg) tuple
    doom.yield_to_human(0)  # cooldown lapses; the entity may move again
    assert doom.entity_command("w")
    assert _wait_for(lambda: "key=up" in doom.frame())
    doom.stop()


def test_module_events_declared(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    hooks = loader.registry.get("hooks")
    assert "doom_start" in hooks.known()
    assert "doom_stop" in hooks.known()
    assert "doom_tick" in hooks.known()


def test_module_utterance_start_and_command(tmp_path, monkeypatch):
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    hooks = loader.registry.get("hooks")
    doom = loader.registry.get("doom")
    hooks.emit("utterance", "doom.start(2)")
    assert doom.running() is True
    assert _wait_for(lambda: "skill 2" in doom.status())
    hooks.emit("utterance", 'The command is: doom.command("shoot").')
    assert _wait_for(lambda: "HIT!" in doom.frame())
    doom.stop()


def test_module_utterance_command_variants(tmp_path, monkeypatch):
    """The hook must tolerate the sloppy doom.command lines a small model
    actually writes: trailing punctuation, inline comments, missing quotes.
    The strafe between d-variants resets the stub's key marker so every
    variant must land fresh."""
    loader = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    hooks = loader.registry.get("hooks")
    commands = loader.registry.get("commands")
    doom = loader.registry.get("doom")
    commands.dispatch("/doom", ["start"])
    assert _wait_for(lambda: doom.frame_count() > 0)
    for variant in (
        'doom.command("d")',
        'doom.command("d").',
        'doom.command("d") -- turning right',
        "doom.command(d)",
    ):
        hooks.emit("utterance", 'doom.command("q")')  # reset marker to strafe-left
        assert _wait_for(lambda: "key=strafe-left" in doom.frame())
        hooks.emit("utterance", variant)
        assert _wait_for(lambda: "key=right" in doom.frame()), f"dropped: {variant!r}"
    for variant in (
        'doom.command( "shoot" ) with trailing prose',
        'The command is: doom.command("shoot")',
    ):
        hooks.emit("utterance", variant)
        assert _wait_for(lambda: "HIT!" in doom.frame()), f"dropped: {variant!r}"
    doom.stop()
