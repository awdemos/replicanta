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

STUB = str(Path(__file__).parent / "fixtures" / "doom_ascii_stub.py")


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
