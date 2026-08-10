#!/bin/bash
# Record a real Replicanta TUI session to an asciinema cast.
# Uses a throwaway nursery (the real one stays clean) and the fast model.
set -euo pipefail

SESSION="replicanta-gif"
CAST="docs/gifs/replicanta_demo.cast"
COLS=120
ROWS=40
MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"

# Compute repo root from this script's location (docs/gifs/record_demo.sh).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Throwaway demo nursery: three organisms, one already in a group so the
# sidebar shows group headers from the first frame.
DEMO_DIR="$(mktemp -d)"
trap 'rm -rf "$DEMO_DIR"; tmux kill-session -t "$SESSION" 2>/dev/null || true' EXIT
cp "$REPO/organism.scl" "$DEMO_DIR/"
PYTHONPATH="$REPO" "$REPO/.venv/bin/python" - "$DEMO_DIR" <<'PY'
import sys
import nursery
root = sys.argv[1]
seed = f"{root}/organism.scl"
nursery.create(root, "fern", seed)
nursery.create(root, "stephanie", seed)
nursery.create_group(root, "dreamers")
nursery.assign(root, "stephanie", "dreamers")
PY

# Start the TUI in a detached tmux session.
tmux kill-session -t "$SESSION" 2>/dev/null || true
tmux new-session -d -s "$SESSION" -x "$COLS" -y "$ROWS" \
    "cd $REPO && PYTHONPATH=. OLLAMA_MODEL=$MODEL .venv/bin/replicanta --dir $DEMO_DIR --chaos 0"

# Send keys to the session in the background.
(
    sleep 3
    # Tour the tabs: mind, memory, inner, back to chat.
    tmux send-keys -t "$SESSION" F3
    sleep 1
    tmux send-keys -t "$SESSION" F4
    sleep 1
    tmux send-keys -t "$SESSION" F7
    sleep 1
    tmux send-keys -t "$SESSION" F2
    sleep 1

    # Chat — each reply is a full thought-arena debate (~6s on the fast model).
    tmux send-keys -t "$SESSION" "hello little one" Enter
    sleep 12
    tmux send-keys -t "$SESSION" "my name is Alex" Enter
    sleep 12

    # Cells tab: the neural memory grid with its color-swatch legend.
    tmux send-keys -t "$SESSION" F8
    sleep 3
    tmux send-keys -t "$SESSION" F2
    sleep 1

    # Group chat: /group start seats fern alongside the organism you live
    # with; everything typed is broadcast, 'fern: …' addresses one member.
    tmux send-keys -t "$SESSION" "/group start fern" Enter
    sleep 2
    tmux send-keys -t "$SESSION" "hi everyone, this is Alex" Enter
    sleep 10
    tmux send-keys -t "$SESSION" "fern: what do you dream about?" Enter
    sleep 7
    tmux send-keys -t "$SESSION" "/group stop" Enter
    sleep 1

    # MUD: direct user moves, then a few auto-turns.
    tmux send-keys -t "$SESSION" "/mud" Enter
    sleep 3
    tmux send-keys -t "$SESSION" "go north" Enter
    sleep 2
    tmux send-keys -t "$SESSION" "take torch" Enter
    sleep 12

    # Map and story.
    tmux send-keys -t "$SESSION" "/mud map" Enter
    sleep 2
    tmux send-keys -t "$SESSION" "/mud story" Enter
    sleep 2

    # Stop MUD, quit with the slash command.
    tmux send-keys -t "$SESSION" "/mud" Enter
    sleep 2
    tmux send-keys -t "$SESSION" "/quit" Enter
    sleep 2
) &

# Record the attached tmux session.
asciinema rec --overwrite --window-size "${COLS}x${ROWS}" \
    --command "tmux attach-session -t $SESSION" "$CAST"
