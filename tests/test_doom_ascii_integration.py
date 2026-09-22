"""Integration test against a real doom-ascii binary.

Skips unless a real binary and WAD resolve (after
scripts/setup_doom_ascii.sh, or with DOOM_ASCII_BIN/DOOM_WAD set). The
stub-based suite in test_doom_ascii.py covers the service hermetically;
this only proves the wire-protocol assumptions (pty, fd-2 input, frame
splitting) hold against the real game.
"""

import time

import pytest

from replicanta import doom_ascii

_BINARY = doom_ascii._find_binary()
_WAD = doom_ascii._find_wad()
_IS_STUB = bool(_BINARY and "doom_ascii_stub" in _BINARY)

pytestmark = pytest.mark.skipif(
    not _BINARY or not _WAD or _IS_STUB,
    reason="real doom-ascii binary + WAD not installed",
)


def test_real_game_starts_moves_and_stops():
    svc = doom_ascii.DoomAsciiService()
    try:
        svc.start()
        assert svc.running() is True
        assert svc.frame()
        svc.command("w")
        time.sleep(0.5)
        assert svc.frame_count() > 0
    finally:
        svc.stop()
    assert svc.running() is False
