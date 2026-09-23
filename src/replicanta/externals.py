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


def wetware_binary(root: str | None = None) -> str | None:
    """Path to the rsi-wetware-rs CLI, or None.

    Search order: WETWARE_BIN, then the usual cargo target locations under
    the nursery root's parent and ~/code (preferring the richer
    rsi-wetware-rs over minimal wetware-rs, release over debug), then PATH.
    """
    env = os.environ.get("WETWARE_BIN")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    bases = []
    if root is not None:
        bases.append(Path(root).parent)
    bases.append(Path.home() / "code")
    seen = set()
    for base in bases:
        for profile in ("release", "debug"):
            for repo in ("rsi-wetware-rs", "wetware-rs"):
                cand = base / repo / "target" / profile / "wetware"
                if cand in seen:
                    continue
                seen.add(cand)
                if cand.is_file() and os.access(cand, os.X_OK):
                    return str(cand)
    return shutil.which("wetware")


class ExternalsService:
    """Registry-facing facade so Lua modules can ask 'is X installed?'."""

    def __init__(self, root: str | None = None):
        self._root = root

    def doom_binary(self) -> str | None:
        return doom_binary()

    def doom_wad(self) -> str | None:
        return doom_wad()

    def doom_args(self) -> str:
        """Raw DOOM_ASCII_ARGS string (test/debug knob); Lua splits it."""
        return os.environ.get("DOOM_ASCII_ARGS", "")

    def wetware_binary(self) -> str | None:
        return wetware_binary(self._root)

    def available(self, name: str) -> bool:
        return {
            "doom": doom_binary() is not None and doom_wad() is not None,
            "wetware": self.wetware_binary() is not None,
        }.get(str(name), False)
