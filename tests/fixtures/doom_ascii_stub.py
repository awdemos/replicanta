#!/usr/bin/env python3
"""Test double for the doom-ascii binary (see src/replicanta/doom_ascii.py).

Speaks the same wire protocol: termios on fd 0 (must be a tty), keystrokes
read from fd 2, frames written to stdout as cursor-home + 25 rows of 80
chars + SGR reset. The title is wrapped in truecolor SGR so tests can
assert colors survive to the pane. Reacts to keys so tests can assert
cause and effect:
space -> HIT marker, e -> OPEN marker, x -> clean exit. Game args (-iwad,
-scaling, -skill) are accepted and ignored except -skill, which is echoed
into the scene. Exits after --frames N frames if given.

Run manually under a pty, e.g.:
    python doom_ascii_stub.py --frames 20
"""

from __future__ import annotations

import argparse
import os
import sys
import time

ROWS = 25
COLS = 80

CURSOR_HOME = "\x1b[;H"
SGR_RESET = "\x1b[0m"
CLEAR_FIRST = "\x1b[1;1H\x1b[2J"

ARROWS = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--frames", type=int, default=0)
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("-iwad")
    parser.add_argument("-scaling")
    parser.add_argument("-skill", default="1")
    parser.add_argument("-chars")
    parser.add_argument("-nocolor", action="store_true")
    parser.add_argument("-nograd", action="store_true")
    parser.add_argument("-nobold", action="store_true")
    parser.add_argument("-erase", action="store_true")
    parser.add_argument("-kpsmooth")
    parser.add_argument("-fixgamma", action="store_true")
    parser.add_argument("--die", action="store_true")
    args, unknown = parser.parse_known_args()
    # Unknown flags (e.g. the module's -warp 1 1) are echoed into the scene
    # so tests can assert the exact argv the module spawned with.
    args.unknown = " ".join(unknown)
    return args


def main() -> int:
    args = parse_args()
    try:
        import termios

        saved = termios.tcgetattr(0)
        raw = termios.tcgetattr(0)
        raw[3] &= ~(termios.ECHO | termios.ICANON)  # lflag, like DG_ReadInput
        raw[6][termios.VMIN] = 0  # non-blocking reads, like the real game
        raw[6][termios.VTIME] = 0
        termios.tcsetattr(0, termios.TCSANOW, raw)
    except termios.error:
        saved = None

    last_key = "none"
    hit_ttl = 0
    open_ttl = 0
    frame_no = 0
    exiting = False
    out = sys.stdout
    out.write(CLEAR_FIRST)
    if args.die:
        # Mimic a real game that cannot start: error to stderr (fd 2, the
        # pty slave), then linger without frames until killed.
        os.write(2, b"Wad file fake.wad doesn't have IWAD or PWAD id\r\n")
        while True:
            time.sleep(3600)
    try:
        while True:
            try:
                if os.isatty(2):
                    data = os.read(2, 64)
                else:
                    data = b""
            except OSError:
                data = b""
            for seq, name in ARROWS.items():
                if seq in data:
                    last_key = name
            for byte in data:
                ch = chr(byte)
                if ch == "x":
                    exiting = True
                elif ch == " ":
                    last_key = "fire"
                    hit_ttl = 6
                elif ch == "e":
                    last_key = "use"
                    open_ttl = 6
                elif ch == ",":
                    last_key = "strafe-left"
                elif ch == ".":
                    last_key = "strafe-right"
                elif ch in "1234567":
                    last_key = f"weapon{ch}"
            frame_no += 1
            out.write(CURSOR_HOME + scene(args, frame_no, last_key, hit_ttl, open_ttl) + SGR_RESET)
            out.flush()
            if hit_ttl > 0:
                hit_ttl -= 1
            if open_ttl > 0:
                open_ttl -= 1
            if exiting or (args.frames and frame_no >= args.frames):
                break
            time.sleep(args.interval)
    finally:
        if saved is not None:
            import termios

            termios.tcsetattr(0, termios.TCSANOW, saved)
        out.write("\x1b[?25h\n")
        out.flush()
    return 0


def scene(args: argparse.Namespace, frame_no: int, last_key: str, hit_ttl: int, open_ttl: int) -> str:
    import re

    def visible_len(s):
        return len(re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s))

    lines = []
    inner = [
        f"\x1b[38;2;255;0;0mDOOM-ASCII STUB\x1b[0m skill={args.skill} scaling={args.scaling} chars={args.chars} frame={frame_no:04d} key={last_key}",
        f"args={args.unknown} nograd={args.nograd} fixgamma={args.fixgamma}",
        "",
        "   @",
        "  ###",
    ]
    if hit_ttl:
        inner.append("HIT! demon down")
    if open_ttl:
        inner.append("OPEN! door slides")
    lines.append("#" * COLS)
    for text in inner:
        pad = " " * max(0, COLS - 2 - visible_len(text))
        lines.append("#" + text + pad + "#")
    while len(lines) < ROWS - 1:
        lines.append("#" + " " * (COLS - 2) + "#")
    lines.append("#" * COLS)
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
