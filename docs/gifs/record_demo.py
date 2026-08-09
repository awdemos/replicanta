"""Drive the Replicanta TUI for an asciinema/agg GIF recording."""
import os
import sys
import time

import pexpect

# Terminal size used by asciinema: --window-size 120x40
ROWS = 40
COLS = 120

# Function-key escape sequences (xterm-like, which Textual expects).
F2 = "\x1b[OQ"
F3 = "\x1b[OR"
F4 = "\x1b[OS"
F7 = "\x1b[18~"
CTRL_Q = "\x11"

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def wait_for(child, needle, timeout=10):
    try:
        child.expect(needle, timeout=timeout)
    except (pexpect.TIMEOUT, pexpect.EOF):
        # Keep going rather than crashing the recording.
        pass


def main():
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    env["TERM"] = "xterm-256color"

    child = pexpect.spawn(
        os.path.join(REPO, ".venv/bin/replicanta"),
        ["--chaos", "0"],
        cwd=REPO,
        dimensions=(ROWS, COLS),
        timeout=30,
        encoding=None,
        env=env,
    )
    # Echo the TUI's output to stdout so asciinema captures it.
    child.logfile = sys.stdout.buffer

    # Wait for the TUI to render the status bar / placeholder.
    wait_for(child, b"F2 chat")
    time.sleep(1.0)

    # Switch through tabs.
    child.send(F3)
    time.sleep(1.0)
    child.send(F4)
    time.sleep(1.0)
    child.send(F7)
    time.sleep(1.0)
    child.send(F2)
    time.sleep(1.0)

    # Type a couple of chat lines.
    child.sendline("hello")
    time.sleep(2.0)
    child.sendline("my name is Alex")
    time.sleep(2.0)

    # Toggle MUD and demonstrate direct user commands.
    child.sendline("/mud")
    time.sleep(2.0)
    child.sendline("go north")
    time.sleep(2.0)
    child.sendline("take torch")
    time.sleep(2.0)

    # Let the organism auto-play a couple of turns.
    time.sleep(8.0)

    # Show map and story.
    child.sendline("/mud map")
    time.sleep(1.0)
    child.sendline("/mud story")
    time.sleep(1.0)

    # Pause, step, resume.
    child.sendline("/mud pause")
    time.sleep(1.0)
    child.sendline("/mud step")
    time.sleep(2.0)
    child.sendline("/mud resume")
    time.sleep(1.0)

    # Stop MUD.
    child.sendline("/mud")
    time.sleep(2.0)

    # Quit.
    child.send(CTRL_Q)
    time.sleep(1.0)
    child.close(force=True)


if __name__ == "__main__":
    main()
