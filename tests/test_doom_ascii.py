"""Service-level tests for the doom-ascii capability bridge.

Hermetic: DOOM_ASCII_BIN points at the committed stub binary double (see
tests/fixtures/doom_ascii_stub.py), so no C toolchain, network, or WAD is
needed. Real-binary integration lives in test_doom_ascii_integration.py.
"""

import os
import time
from pathlib import Path

import pytest

from replicanta import doom_ascii
from replicanta.modules import ModuleLoader

STUB = str(Path(__file__).parent / "fixtures" / "doom_ascii_stub.py")
MODULES_SRC = Path(__file__).parent.parent / "modules"


@pytest.fixture
def stub_env(tmp_path, monkeypatch):
    wad = tmp_path / "doom1.wad"
    wad.write_bytes(b"PWAD fake-for-stub")
    monkeypatch.setenv("DOOM_ASCII_BIN", STUB)
    monkeypatch.setenv("DOOM_WAD", str(wad))
    monkeypatch.setenv("DOOM_ASCII_ARGS", "--interval 0.02")
    svc = doom_ascii.DoomAsciiService()
    yield svc
    svc.stop()


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- pure protocol helpers ------------------------------------------------------


def test_split_frames_complete_and_pending():
    body = "line1\nline2" + doom_ascii.SGR_RESET
    frames, pending = doom_ascii.split_frames(
        "\x1b[1;1H\x1b[2J" + doom_ascii.CURSOR_HOME + body + doom_ascii.CURSOR_HOME + "par"
    )
    assert frames == [body]
    assert pending == "par"


def test_split_frames_drops_non_frame_chunks():
    # The first-frame clear prefix is not a frame; incomplete tails wait.
    frames, pending = doom_ascii.split_frames("\x1b[1;1H\x1b[2J" + doom_ascii.CURSOR_HOME)
    assert frames == []
    assert pending == ""


def test_strip_ansi_removes_sgr_and_osc():
    raw = "\x1b]2;DOOM\x07\x1b[38;2;255;000;000m#\x1b[0m\x1b[;H"
    assert doom_ascii.strip_ansi(raw) == "#"


def test_keymap_covers_documented_vocabulary():
    for cmd in ("w", "s", "a", "d", "q", "e", "shoot", "use", "open", "1", "7"):
        assert cmd in doom_ascii.KEYMAP
    assert doom_ascii.KEYMAP["shoot"] == b" "
    assert doom_ascii.KEYMAP["w"] == b"\x1b[A"


# -- availability -----------------------------------------------------------------


def test_available_with_stub_env(stub_env):
    assert stub_env.available() is True


def test_unavailable_without_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(doom_ascii, "_find_binary", lambda: None)
    monkeypatch.setattr(doom_ascii, "_find_wad", lambda: str(tmp_path / "x.wad"))
    assert doom_ascii.DoomAsciiService().available() is False


def test_start_raises_helpful_error_when_not_built(monkeypatch, tmp_path):
    monkeypatch.setattr(doom_ascii, "_find_binary", lambda: None)
    monkeypatch.setattr(doom_ascii, "_find_wad", lambda: str(tmp_path / "x.wad"))
    with pytest.raises(RuntimeError, match="setup_doom_ascii"):
        doom_ascii.DoomAsciiService().start()


# -- lifecycle ---------------------------------------------------------------------


def test_start_returns_first_frame_and_runs(stub_env):
    out = stub_env.start()
    assert "DOOM-ASCII STUB" in out
    assert stub_env.running() is True
    assert "#" in stub_env.frame()


def test_start_passes_skill_to_game(stub_env):
    out = stub_env.start(3)
    assert "skill=3" in out
    assert "skill 3" in stub_env.status()


def test_start_coerces_garbage_skill(stub_env):
    out = stub_env.start("default")  # old nano-doom callers passed a map name
    assert "skill=1" in out


def test_status_without_game(stub_env):
    assert "no game running" in stub_env.status()


def test_stop_halts_game_and_reaps_process(stub_env):
    stub_env.start()
    proc = stub_env._proc
    pid = proc.pid
    assert stub_env.stop() == "stopped"
    assert stub_env.running() is False
    assert "no game running" in stub_env.status()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # reaped: no orphan left behind


def test_game_exit_is_detected(stub_env, monkeypatch):
    monkeypatch.setenv("DOOM_ASCII_ARGS", "--frames 5 --interval 0.02")
    stub_env.start()
    assert _wait_for(lambda: not stub_env.running(), timeout=5.0)
    assert "game over" in stub_env.status()


def test_start_while_running_replaces_game(stub_env):
    stub_env.start()
    first_pid = stub_env._proc.pid
    stub_env.start()
    assert stub_env.running() is True
    with pytest.raises(ProcessLookupError):
        os.kill(first_pid, 0)


# -- commands -----------------------------------------------------------------------


def test_command_shoot_marks_hit(stub_env):
    stub_env.start()
    stub_env.command("shoot")
    assert _wait_for(lambda: "HIT!" in stub_env.frame())


def test_command_use_marks_open(stub_env):
    stub_env.start()
    stub_env.command("use")
    assert _wait_for(lambda: "OPEN!" in stub_env.frame())


def test_command_movement_reaches_game(stub_env):
    stub_env.start()
    stub_env.command("w")
    assert _wait_for(lambda: "key=up" in stub_env.frame())


def test_command_weapon_select(stub_env):
    stub_env.start()
    stub_env.command("3")
    assert _wait_for(lambda: "key=weapon3" in stub_env.frame())


def test_command_unknown_raises(stub_env):
    stub_env.start()
    with pytest.raises(ValueError, match="unknown doom command"):
        stub_env.command("fly")


def test_command_without_game_raises(stub_env):
    with pytest.raises(RuntimeError, match="no game running"):
        stub_env.command("w")


# -- module integration (real Lua module dir, stub binary) ------------------------


def _load_module(tmp_path, monkeypatch, enabled):
    import shutil

    wad = tmp_path / "doom1.wad"
    if not wad.exists():
        wad.write_bytes(b"PWAD fake")
    monkeypatch.setenv("DOOM_ASCII_BIN", STUB)
    monkeypatch.setenv("DOOM_WAD", str(wad))
    monkeypatch.setenv("DOOM_ASCII_ARGS", "--interval 0.02")
    target = tmp_path / "modules"
    shutil.copytree(MODULES_SRC, target)
    logs = []
    loader = ModuleLoader(target, organism=None, modules_config={"enabled": enabled}, emit=logs.append)
    loader.load_all()
    return loader, logs


def test_module_loads_and_registers_doom(tmp_path, monkeypatch):
    loader, _ = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    assert "doom-ascii" in loader.modules
    assert loader.registry.get("doom") is not None


def test_module_config_alias_nano_doom(tmp_path, monkeypatch):
    # Existing organism configs still say "nano-doom"; the loader maps it.
    loader, _ = _load_module(tmp_path, monkeypatch, ["base", "nano-doom"])
    assert "doom-ascii" in loader.modules


def test_module_slash_command_status_and_start(tmp_path, monkeypatch):
    loader, _ = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    commands = loader.registry.get("commands")
    assert "no game running" in commands.dispatch("/doom", [])
    result = commands.dispatch("/doom", ["start"])
    assert "doom-ascii" in result
    doom = loader.registry.get("doom")
    assert doom.running() is True
    doom.stop()


def test_module_slash_command_direct_move(tmp_path, monkeypatch):
    loader, _ = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    commands = loader.registry.get("commands")
    commands.dispatch("/doom", ["start"])
    doom = loader.registry.get("doom")
    commands.dispatch("/doom", ["w"])
    assert _wait_for(lambda: "key=up" in doom.frame())
    doom.stop()


def test_module_frame_api(tmp_path, monkeypatch):
    loader, _ = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    commands = loader.registry.get("commands")
    commands.dispatch("/doom", ["start"])
    doom = loader.registry.get("doom")
    assert "DOOM-ASCII STUB" in doom.frame()
    assert "DOOM-ASCII STUB" in doom.frame_ansi()
    assert doom.frame_count() > 0
    doom.stop()


def test_module_events_declared(tmp_path, monkeypatch):
    loader, _ = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    hooks = loader.registry.get("hooks")
    assert "doom_start" in hooks.known()
    assert "doom_stop" in hooks.known()
    assert "doom_tick" in hooks.known()


def test_module_utterance_start_and_command(tmp_path, monkeypatch):
    loader, logs = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    hooks = loader.registry.get("hooks")
    doom = loader.registry.get("doom")
    hooks.emit("utterance", "doom.start(2)")
    assert any("doom-ascii: started (skill 2)" in line for line in logs)
    assert doom.running() is True
    hooks.emit("utterance", 'The command is: doom.command("shoot").')
    assert _wait_for(lambda: "HIT!" in doom.frame())
    doom.stop()


def test_module_utterance_command_variants(tmp_path, monkeypatch):
    """The hook must tolerate the sloppy doom.command lines a small model
    actually writes: trailing punctuation, inline comments, missing quotes.
    A variant that is parsed but never reaches the game fails the marker
    wait; the strafe between d-variants resets the stub's key marker so
    every variant must land fresh."""
    loader, _ = _load_module(tmp_path, monkeypatch, ["base", "doom-ascii"])
    hooks = loader.registry.get("hooks")
    commands = loader.registry.get("commands")
    doom = loader.registry.get("doom")
    commands.dispatch("/doom", ["start"])
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
