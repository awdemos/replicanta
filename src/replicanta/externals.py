"""Discovery of external binaries and data files (doom-ascii, wetware).

Python keeps *finding* external things; Lua modules keep everything else.
These helpers back the built-in ``externals`` service that pure-Lua modules
consume, and honor the same environment overrides the test suite uses
(DOOM_ASCII_BIN, DOOM_WAD, WETWARE_BIN).
"""

from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path

SHAREWARE_WAD_SIZE = 4_196_020  # doom1.wad v1.9, bytes

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def doom_binary() -> str | None:
    """Path to the doom-ascii game binary, or None when not built."""
    env = os.environ.get("DOOM_ASCII_BIN")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    matches = sorted(
        glob.glob(str(_REPO_ROOT / ".deps" / "doom-ascii" / "_*" / "game" / "doom_ascii"))
        + glob.glob(str(_REPO_ROOT / ".deps" / "doom-ascii" / "_*" / "game" / "doom-ascii")),
        key=os.path.getmtime,
    )
    for candidate in reversed(matches):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("doom_ascii") or shutil.which("doom-ascii")


def doom_wad() -> str | None:
    """Path to a usable DOOM WAD (shareware doom1.wad preferred)."""
    env = os.environ.get("DOOM_WAD")
    if env and os.path.isfile(env):
        return env
    candidates = sorted(glob.glob(str(_REPO_ROOT / ".deps" / "*.wad")))
    candidates += sorted(glob.glob(str(Path.home() / ".local" / "share" / "replicanta" / "*.wad")))
    doomwaddir = os.environ.get("DOOMWADDIR")
    if doomwaddir:
        candidates += sorted(glob.glob(str(Path(doomwaddir) / "*.wad")))
    candidates.sort(key=lambda p: ("doom1" not in os.path.basename(p).lower(), p))
    return candidates[0] if candidates else None


def wetware_binary() -> str | None:
    """Path to the rsi-wetware-rs CLI, or None."""
    env = os.environ.get("WETWARE_BIN")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    return shutil.which("wetware")


class ExternalsService:
    """Registry-facing facade so Lua modules can ask 'is X installed?'."""

    def doom_binary(self) -> str | None:
        return doom_binary()

    def doom_wad(self) -> str | None:
        return doom_wad()

    def wetware_binary(self) -> str | None:
        return wetware_binary()

    def available(self, name: str) -> bool:
        return {
            "doom": doom_binary() is not None and doom_wad() is not None,
            "wetware": wetware_binary() is not None,
        }.get(str(name), False)
