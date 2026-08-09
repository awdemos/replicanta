#!/bin/bash
# Record a real Replicanta TUI session to an asciinema cast.
set -euo pipefail

SESSION="replicanta-gif"
CAST="docs/gifs/replicanta_demo.cast"
COLS=120
ROWS=40

# Compute repo root from this script's location (docs/gifs/record_demo.sh).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Start the TUI in a detached tmux session.
tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -x "$COLS" -y "$ROWS" \
    "cd $REPO && PYTHONPATH=. .venv/bin/replicanta --chaos 0"

# Send keys to the session in the background.
(
    sleep 2
    # Switch through tabs.
    tmux send-keys -t "$SESSION" F3
    sleep 1
    tmux send-keys -t "$SESSION" F4
    sleep 1
    tmux send-keys -t "$SESSION" F7
    sleep 1
    tmux send-keys -t "$SESSION" F2
    sleep 1

    # Chat lines.
    tmux send-keys -t "$SESSION" "hello" Enter
    sleep 2
    tmux send-keys -t "$SESSION" "my name is Alex" Enter
    sleep 2

    # Toggle MUD and demonstrate direct user commands.
    tmux send-keys -t "$SESSION" "/mud" Enter
    sleep 2
    tmux send-keys -t "$SESSION" "go north" Enter
    sleep 2
    tmux send-keys -t "$SESSION" "take torch" Enter
    sleep 2

    # Let the organism auto-play a couple of turns.
    sleep 8

    # Show map and story.
    tmux send-keys -t "$SESSION" "/mud map" Enter
    sleep 1
    tmux send-keys -t "$SESSION" "/mud story" Enter
    sleep 1

    # Pause, step, resume.
    tmux send-keys -t "$SESSION" "/mud pause" Enter
    sleep 1
    tmux send-keys -t "$SESSION" "/mud step" Enter
    sleep 2
    tmux send-keys -t "$SESSION" "/mud resume" Enter
    sleep 1

    # Stop MUD.
    tmux send-keys -t "$SESSION" "/mud" Enter
    sleep 2

    # Quit with the slash command.
    tmux send-keys -t "$SESSION" "/quit" Enter
) &

# Record the attached tmux session.
asciinema rec --window-size "${COLS}x${ROWS}" \
    --command "tmux attach-session -t $SESSION" "$CAST"

# Cleanup.
tmux kill-session -t "$SESSION" 2>/dev/null || true
