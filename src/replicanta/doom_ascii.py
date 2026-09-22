"""DOOM through the doom-ascii terminal port (external GPL-2.0 binary).

doom-ascii (https://github.com/wojciech-graj/doom-ascii) is a source port of
doomgeneric that renders real DOOM as text. It is an *optional external*
dependency: Replicanta (MIT) never ships its source or binaries. Build it
locally with ``scripts/setup_doom_ascii.sh`` (clones into gitignored
``.deps/`` and fetches the shareware ``doom1.wad``), or point
``DOOM_ASCII_BIN`` / ``DOOM_WAD`` at your own install. When the binary or a
WAD cannot be found, ``available()`` is False and the module loads but is
not playable — the same contract as the arm/fly-brain bridges.

Wire protocol (verified against doomgeneric_ascii.c):

- The child sets termios on fd 0, so fd 0 must be a tty; we give it a pty
  slave. It *reads* keystrokes from fd 2, so stderr is attached to the same
  slave (one input queue, ECHO off — keystrokes never echo into frames).
- Every frame is a single write starting with the cursor-home escape
  (``ESC [ ; H``), optionally clear/bold, then RESY rows of RESX pixels
  (ASCII mode: two chars per pixel), and a trailing SGR reset. Capturing a
  frame is just "split the stream on cursor-home, keep complete chunks" —
  no terminal emulation.
- A tap is one key write; the game auto-releases after ``-kpsmooth`` ms
  (default 42). Movement commands therefore send a few taps.

The public surface (``available/start/stop/command/status/running`` plus
``frame``) mirrors what the nano-doom Lua module consumed; the old
structured introspection (``tactical``, ``can_shoot``, ``maps``) has no
equivalent in a real game process and is gone — the entity perceives the
screen like a human does: as ASCII art.
"""

from __future__ import annotations

import atexit
import contextlib
import glob
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

SCALING = "8"  # 320/8 x 200/8 px -> 40x25 px -> 80x25 char frame
DEFAULT_SKILL = 1
FIRST_FRAME_TIMEOUT = 8.0
FRAME_PROMPT_CHARS = 1200  # plain-frame budget for LLM prompts
MAX_FRAME_CHARS = 60_000  # ansi-frame budget for the TUI pane

CURSOR_HOME = "\x1b[;H"
SGR_RESET = "\x1b[0m"

# Command vocabulary shared with the Lua utterance hook and the TUI:
# w/s forward-back, a/d turn, q/e strafe, shoot/use, weapon 1-7.
KEYMAP: dict[str, bytes] = {
    "w": b"\x1b[A",
    "s": b"\x1b[B",
    "d": b"\x1b[C",
    "a": b"\x1b[D",
    "q": b",",
    "e": b".",
    "shoot": b" ",
    "fire": b" ",
    "use": b"e",
    "open": b"e",
    "1": b"1",
    "2": b"2",
    "3": b"3",
    "4": b"4",
    "5": b"5",
    "6": b"6",
    "7": b"7",
}
MOVEMENT_COMMANDS = frozenset({"w", "s", "a", "d", "q", "e"})
TAPS_PER_COMMAND = 3  # movement taps, ~50 ms apart, for a meaningful step
TAP_SPACING = 0.05

_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_SGR_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_ACTIVE: set[DoomAsciiService] = set()
_ATEXIT_REGISTERED = False


def _reap_all() -> None:
    for svc in list(_ACTIVE):
        with contextlib.suppress(Exception):  # never block interpreter shutdown
            svc.stop()


def _register_active(svc: DoomAsciiService) -> None:
    global _ATEXIT_REGISTERED
    _ACTIVE.add(svc)
    if not _ATEXIT_REGISTERED:
        _ATEXIT_REGISTERED = True
        atexit.register(_reap_all)


def strip_ansi(text: str) -> str:
    """Remove OSC titles and SGR color/cursor sequences from a frame."""
    return _SGR_RE.sub("", _OSC_RE.sub("", text))


def split_frames(buffer: str) -> tuple[list[str], str]:
    """Split a decoded stream into (complete frames, pending tail).

    Frames arrive back-to-back, each starting with cursor-home; a chunk is
    complete once the next home arrives and it carries the SGR reset the
    engine emits at the end of every frame. The pending tail is whatever
    the last, still in-progress chunk holds.
    """
    parts = buffer.split(CURSOR_HOME)
    pending = parts.pop() if parts else ""
    frames = [p for p in parts if p.endswith(SGR_RESET)]
    return frames, pending


def _find_binary() -> str | None:
    env = os.environ.get("DOOM_ASCII_BIN")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    root = Path(__file__).resolve().parent.parent.parent
    matches = sorted(
        glob.glob(str(root / ".deps" / "doom-ascii" / "_*" / "game" / "doom_ascii"))
        + glob.glob(str(root / ".deps" / "doom-ascii" / "_*" / "game" / "doom-ascii")),
        key=os.path.getmtime,
    )
    for candidate in reversed(matches):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("doom_ascii") or shutil.which("doom-ascii")


def _find_wad() -> str | None:
    env = os.environ.get("DOOM_WAD")
    if env and os.path.isfile(env):
        return env
    root = Path(__file__).resolve().parent.parent.parent
    candidates = sorted(glob.glob(str(root / ".deps" / "*.wad")))
    candidates += sorted(glob.glob(str(Path.home() / ".local" / "share" / "replicanta" / "*.wad")))
    doomwaddir = os.environ.get("DOOMWADDIR")
    if doomwaddir:
        candidates += sorted(glob.glob(str(Path(doomwaddir) / "*.wad")))
    candidates.sort(key=lambda p: ("doom1" not in os.path.basename(p).lower(), p))
    return candidates[0] if candidates else None


class DoomAsciiService:
    """Bridge between the doom Lua module and a doom-ascii child process."""

    def __init__(self, organism=None, lua_lock=None):
        self.organism = organism
        self._lua_lock = lua_lock
        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._reader: threading.Thread | None = None
        self._watcher: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._buffer = ""
        self._slave_log = bytearray()
        self._ansi_frame = ""
        self._plain_frame = ""
        self._frame_count = 0
        self._skill = DEFAULT_SKILL
        self._stopped = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def available(self) -> bool:
        return _find_binary() is not None and _find_wad() is not None

    def start(self, skill: int | str = DEFAULT_SKILL) -> str:
        try:
            self._skill = max(1, min(5, int(skill)))
        except (TypeError, ValueError):
            self._skill = DEFAULT_SKILL
        binary, wad = _find_binary(), _find_wad()
        if binary is None or wad is None:
            raise RuntimeError(
                "doom-ascii not built — run scripts/setup_doom_ascii.sh (or set DOOM_ASCII_BIN and DOOM_WAD)"
            )
        self.stop()
        self._stopped.clear()
        master, slave = os.openpty()
        argv = [
            binary,
            "-iwad",
            wad,
            "-scaling",
            SCALING,
            "-skill",
            str(self._skill),
        ] + shlex.split(os.environ.get("DOOM_ASCII_ARGS", ""))
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=slave,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(slave)
        self._master_fd = master
        self._buffer = ""
        self._slave_log = bytearray()
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="doom-ascii-reader")
        self._reader.start()
        self._watcher = threading.Thread(target=self._watch_loop, daemon=True, name="doom-ascii-watcher")
        self._watcher.start()
        _register_active(self)
        deadline = time.monotonic() + FIRST_FRAME_TIMEOUT
        while time.monotonic() < deadline:
            if self._plain_frame:
                return self._plain_frame
            if self._proc.poll() is not None:
                tail = self._slave_tail()
                raise RuntimeError(
                    f"doom-ascii exited immediately (code {self._proc.returncode})" + (f": {tail}" if tail else "")
                )
            time.sleep(0.02)
        raise RuntimeError("doom-ascii produced no frame in time")

    def stop(self) -> str:
        self._stopped.set()
        proc, self._proc = self._proc, None
        master, self._master_fd = self._master_fd, None
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1.5)
        if master is not None:
            with contextlib.suppress(OSError):
                os.close(master)
        _ACTIVE.discard(self)
        return "stopped"

    # -- state -----------------------------------------------------------------

    def running(self) -> bool:
        return self._proc is not None and not self._stopped.is_set() and self._proc.poll() is None

    def status(self) -> str:
        if self._proc is None:
            return "no game running — /doom start begins DOOM"
        if self.running():
            return f"doom-ascii: skill {self._skill} · frame {self._frame_count} · running — /doom stop ends it"
        return f"doom-ascii: game over after {self._frame_count} frames — /doom start to play again"

    def frame(self) -> str:
        """Latest plain-text screen, capped for LLM prompts."""
        with self._frame_lock:
            return self._plain_frame[:FRAME_PROMPT_CHARS]

    def frame_ansi(self) -> str:
        """Latest raw frame (colors intact), capped for the TUI pane."""
        with self._frame_lock:
            return self._ansi_frame[:MAX_FRAME_CHARS]

    def frame_count(self) -> int:
        return self._frame_count

    # -- input ------------------------------------------------------------------

    def command(self, cmd: str) -> str:
        text = str(cmd or "").strip().lower()
        if text == "start":
            return self.start()
        if not self.running():
            raise RuntimeError("no game running")
        seq = KEYMAP.get(text)
        if seq is None:
            raise ValueError(f"unknown doom command {text!r}")
        taps = TAPS_PER_COMMAND if text in MOVEMENT_COMMANDS else 1
        for _ in range(taps):
            self._tap(seq)
            if taps > 1:
                time.sleep(TAP_SPACING)
        time.sleep(0.03)  # let the game fold the key into its next frame
        return self.frame()

    def _tap(self, seq: bytes) -> None:
        fd = self._master_fd
        if fd is None:
            raise RuntimeError("no game running")
        with self._write_lock:
            os.write(fd, seq)

    # -- background threads -------------------------------------------------------

    def _feed(self, data: bytes) -> None:
        frames, self._buffer = split_frames(self._buffer + data.decode("utf-8", "replace"))
        if not frames:
            return
        with self._frame_lock:
            for raw in frames:
                ansi = _OSC_RE.sub("", raw)
                self._ansi_frame = ansi
                self._plain_frame = strip_ansi(ansi).strip("\n")
            self._frame_count += len(frames)

    def _read_loop(self) -> None:
        import selectors

        stdout = self._proc.stdout if self._proc is not None else None
        if stdout is None:
            return
        selector = selectors.DefaultSelector()
        selector.register(stdout, selectors.EVENT_READ)
        if self._master_fd is not None:
            selector.register(self._master_fd, selectors.EVENT_READ)
        while not self._stopped.is_set():
            try:
                events = selector.select(timeout=0.1)
            except (OSError, ValueError):
                break
            for key, _ in events:
                try:
                    chunk = os.read(key.fd, 65536)
                except OSError:
                    chunk = b""
                if key.fileobj is stdout:
                    if not chunk:
                        self._stopped.set()
                        return
                    self._feed(chunk)
                elif chunk:
                    # The child's stderr is the pty slave; anything written
                    # there (I_Error diagnostics) is kept for error reports.
                    self._slave_log = (self._slave_log + chunk)[-8192:]

    def _watch_loop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        proc.wait()
        self._stopped.set()

    def _slave_tail(self) -> str:
        raw = bytes(self._slave_log).decode("utf-8", errors="replace")
        return " ".join(strip_ansi(raw).split())[-300:]
