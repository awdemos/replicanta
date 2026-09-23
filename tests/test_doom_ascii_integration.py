"""Integration test: the pure-Lua doom-ascii module against the real binary.

Skips unless a real binary and WAD resolve through the externals service
(after scripts/setup_doom_ascii.sh, or with DOOM_ASCII_BIN/DOOM_WAD set).
The stub-based suite in test_doom_ascii.py covers everything hermetically;
this only proves the Lua frame parser and pty wiring hold against the
real game's output.
"""

import shutil
import time
from pathlib import Path

import pytest

from replicanta import externals
from replicanta.modules import ModuleLoader

_BINARY = externals.doom_binary()
_WAD = externals.doom_wad()
_IS_STUB = bool(_BINARY and "doom_ascii_stub" in _BINARY)

pytestmark = pytest.mark.skipif(
    not _BINARY or not _WAD or _IS_STUB,
    reason="real doom-ascii binary + WAD not installed",
)


def _wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_real_game_starts_moves_and_stops(tmp_path):
    target = tmp_path / "modules"
    shutil.copytree(Path(__file__).parent.parent / "modules", target)
    loader = ModuleLoader(target, organism=None, modules_config={"enabled": ["base", "doom-ascii"]})
    loader.load_all()
    doom = loader.registry.get("doom")
    assert doom.start() is True
    try:
        assert _wait_for(lambda: doom.frame_count() > 0)
        assert doom.running() is True
        assert doom.frame()
        doom.command("w")
        assert _wait_for(lambda: doom.frame_count() > 3)
    finally:
        doom.stop()
    assert doom.running() is False
