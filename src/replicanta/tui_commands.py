"""Pure UI helpers for the organism TUI: slash-command registry, tab
completion, activity sparkline, help text. No textual imports — unit
testable without a terminal. Sentiment scorers live in sentiment.py."""

COMMANDS = [
    # State
    ("/chaos", "/chaos 0..1", "set randomness 0-1", "State"),
    ("/focus", "/focus attr", "lock attention on attr (bare /focus clears)", "State"),
    ("/sleep", "/sleep", "force wake->sleep", "State"),
    ("/wake", "/wake", "force sleep->wake", "State"),
    ("/revive", "/revive", "bring a faded organism back", "State"),
    ("/stats", "/stats", "show growth metrics", "State"),
    ("/think", "/think", "narrate thoughts now", "State"),
    ("/self-talk", "/self-talk", "let the organism speak to itself", "State"),
    ("/persona", "/persona [name|off|list]", "activate, clear, or list personas", "State"),
    ("/auto-apply", "/auto-apply [on|off]", "toggle automatic self-patch application", "State"),
    # Voice
    (
        "/voice",
        "/voice [on|off|list|use|get]",
        "spoken voice: toggle, list, switch, download piper voices",
        "Voice",
    ),
    # Senses
    ("/listen", "/listen", "push-to-talk: start/stop the mic, speak to it (F5)", "Senses"),
    (
        "/microphone",
        "/microphone [list|use dev]",
        "mic status, list input devices, choose one",
        "Senses",
    ),
    ("/look", "/look", "grab a camera frame and see it (F6)", "Senses"),
    ("/camera", "/camera [list|use dev]", "camera status, list devices, choose one", "Senses"),
    # MUD
    (
        "/mud",
        "/mud [map|story|quest|pause|resume|step|reset|scenario d…]",
        "toggle or control the dungeon crawl (text adventure)",
        "MUD",
    ),
    # Organisms
    ("/new", "/new [name]", "birth a new organism and swap to it", "Organisms"),
    ("/swap", "/swap name", "swap to another organism", "Organisms"),
    ("/organisms", "/organisms", "list all organisms", "Organisms"),
    (
        "/group",
        "/group start a b | stop",
        "group chat: organisms talk with you and each other",
        "Organisms",
    ),
    # System
    ("/export", "/export [path]", "save chat log to a markdown file", "System"),
    ("/save", "/save", "persist state + genome", "System"),
    ("/modules", "/modules [manage]", "open module manager (or list via /modules)", "System"),
    ("/approve", "/approve", "apply the organism's pending genome patch (manual mode)", "System"),
    ("/reject", "/reject", "discard the pending genome patch (manual mode)", "System"),
    ("/revert", "/revert", "undo the last applied genome patch", "System"),
    ("/reload", "/reload", "re-read the lua hook scripts", "System"),
    ("/lua", "/lua name.lua", "run a lua script from scripts/ on demand", "System"),
    ("/git", "/git [on|off|status]", "toggle or show git sensing", "System"),
    ("/quit", "/quit", "save and quit (same as F10)", "System"),
    # Help
    ("/help", "/help", "this help screen", "Help"),
]

COMMAND_NAMES = [c[0] for c in COMMANDS]


def palette_items():
    """Return all slash commands as (name, usage, description, category)."""
    return COMMANDS


def filter_commands(query):
    """Return commands whose name, usage, or description matches query."""
    q = query.lower().strip()
    if not q:
        return COMMANDS
    return [c for c in COMMANDS if any(q in part.lower() for part in c[:3])]


_SPARK_BARS = "▁▂▃▄▅▆▇█"

CHAT_HISTORY_LIMIT = 50


def complete_command(value, index=0):
    """Tab-cycle slash completion. `index` is the previously used match
    index (0 = first match). Returns (completed_value, next_index)."""
    token = value.split()[0] if value.strip() else ""
    if not token.startswith("/"):
        return value, index
    matches = [n for n in COMMAND_NAMES if n.startswith(token)]
    if not matches:
        return value, index
    used = index % len(matches)
    return matches[used] + value[len(token) :], (used + 1) % len(matches)


def history_push(history, text):
    """Remember a submitted chat line, deduped against the previous line."""
    text = text.strip()
    if not text or (history and history[-1] == text):
        return history
    history.append(text)
    if len(history) > CHAT_HISTORY_LIMIT:
        del history[: len(history) - CHAT_HISTORY_LIMIT]
    return history


def history_browse(history, index, draft, current, delta):
    """Move through chat history. `index` is the browse position (-1 = not
    browsing), `draft` the input value saved when browsing started, `current`
    the live input value, `delta` -1 = older (up) / +1 = newer (down).
    Returns (new_index, draft, value) where `value` is the text to show or
    None when the input should stay untouched."""
    n = len(history)
    if n == 0:
        return index, draft, None
    if index == -1:
        if delta > 0:
            return index, draft, None
        draft = current
        index = n - 1
    else:
        target = index + delta
        if target < 0:
            return index, draft, None
        if target >= n:
            return -1, draft, draft
        index = target
    return index, draft, history[index]


def sparkline(values):
    """One-line histogram of recent activity (belief counts)."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK_BARS[0] * len(values)
    return "".join(
        _SPARK_BARS[int((v - lo) / (hi - lo) * (len(_SPARK_BARS) - 1) + 0.5)]
        for v in values
    )


def help_text():
    lines = ["REPLICANTA — type / in the chat line; tab completes.", ""]
    lines += [f"{usage:<14} {desc}" for _name, usage, desc, _category in COMMANDS]
    lines += [
        "",
        "keyboard",
        "ctrl+p  command palette",
        "F1      this help",
        "F2/F3/F4 chat / mind / memory tabs",
        "F5       push-to-talk (same as /listen)",
        "F6       look through the camera (same as /look)",
        "F7       inner tab: mental-state gauges + thought metabolism",
        "F8       cells tab: top-down neural memory grid (click a cell)",
        "F9       module manager: enable/disable Lua modules",
        "ctrl+s   save now",
        "ctrl+t   think now",
        "tab     complete a slash command",
        "up/down recall previous chat lines",
        "click   a sidebar organism for its menu (swap / rename / move",
        "        to group); click a group header for the group menu",
        "drag    a sidebar organism onto a group (empty space ungroups)",
        "rclick  a group header to rename it; right-click empty sidebar",
        "        space to create a group",
        "F10     quit (ctrl+q too, but terminals may eat it via flow control;",
        "        ctrl+c twice also works)",
        "",
        "mud: /mud toggles; while it runs, type moves directly",
        "(go north, take torch, look, inventory) or prose as a hint.",
        "/mud map|story|quest show the world; /mud pause|resume|step",
        "control auto-turns; /mud scenario <description> dreams up a new",
        "adventure; /mud reset restarts the current one.",
        "",
        "group: /group start fern willow (or 'all', or a nursery group",
        "name) opens a shared chat; everything you type is broadcast to",
        "every member, and each one answers in turn. Address a single",
        "member with 'fern: …' or",
        "'@fern …'. /group stop ends it (members keep their memories).",
        "",
        "scripting: drop .lua files in scripts/ (see scripts/example.lua);",
        "they get on_birth/on_cycle/on_learned/on_utterance/on_fade(ctx)",
        "called at those moments — /reload re-reads them, /lua name.lua",
        "runs one's main(ctx) on demand.",
        "",
        "git sensing: /git on|off toggles whether the organism feels the",
        "worktree state; /git status shows the current repo summary.",
    ]
    return "\n".join(lines)
