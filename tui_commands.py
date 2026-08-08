"""Pure UI helpers for the organism TUI: slash-command registry, tab
completion, activity sparkline, help text. No textual imports — unit
testable without a terminal."""

COMMANDS = [
    ("/chaos", "/chaos 0..1", "set randomness 0-1"),
    ("/focus", "/focus attr", "lock attention on attr (bare /focus clears)"),
    ("/sleep", "/sleep", "force wake->sleep"),
    ("/wake", "/wake", "force sleep->wake"),
    ("/stats", "/stats", "show growth metrics"),
    ("/save", "/save", "persist state + genome"),
    ("/think", "/think", "narrate thoughts now"),
    ("/help", "/help", "this help screen"),
]

COMMAND_NAMES = [c[0] for c in COMMANDS]

_SPARK_BARS = "▁▂▃▄▅▆▇█"

CHAT_HISTORY_LIMIT = 50

HARSHNESS_CAP = 0.15
_HARSH_HITS = 0.03
_HARSH_TERMS = (
    "stupid", "useless", "idiot", "pathetic", "worthless", "dumb",
    "moron", "loser", "shut up", "hate you", "ugly", "screw you",
    "disgusting", "annoying", "trash", "garbage", "suck",
)


def harshness(text):
    """Score how harsh a user message is, 0.0 (neutral) .. HARSHNESS_CAP."""
    low = text.lower()
    hits = sum(1 for term in _HARSH_TERMS if term in low)
    return min(HARSHNESS_CAP, hits * _HARSH_HITS)


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
    return matches[used] + value[len(token):], (used + 1) % len(matches)


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
        for v in values)


def help_text():
    lines = ["ORGANISM — type / in the chat line; tab completes.", ""]
    lines += [f"{usage:<14} {desc}" for _name, usage, desc in COMMANDS]
    lines += [
        "",
        "keyboard",
        "ctrl+p  command palette",
        "F1      this help",
        "ctrl+s  save now",
        "ctrl+t  think now",
        "tab     complete a slash command",
        "up/down recall previous chat lines",
        "q       quit",
    ]
    return "\n".join(lines)
