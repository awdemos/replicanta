import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tui_commands import (
    COMMAND_NAMES,
    complete_command,
    help_text,
    history_browse,
    history_push,
    sparkline,
)


def test_complete_first_match():
    value, index = complete_command("/", 0)
    assert value == COMMAND_NAMES[0]
    assert index == 1


def test_complete_cycles_with_wrap():
    names = [n for n in COMMAND_NAMES if n.startswith("/s")]
    value, index = complete_command("/s", 0)
    assert value == names[0]
    value, index = complete_command("/s", index)
    assert value == names[1]
    value, index = complete_command("/s", index)
    assert value == names[2]
    value, index = complete_command("/s", index)
    assert value == names[0]  # wrapped


def test_complete_partial_preserves_args():
    value, _index = complete_command("/cha", 0)
    assert value == "/chaos"
    value, _index = complete_command("/chaos 0.3", 0)
    assert value == "/chaos 0.3"


def test_complete_plain_text_unchanged():
    value, index = complete_command("hello world", 3)
    assert value == "hello world"
    assert index == 3


def test_complete_no_match_unchanged():
    value, index = complete_command("/zzz", 0)
    assert value == "/zzz"
    assert index == 0


def test_sparkline_flat():
    assert sparkline([3, 3, 3]) == "▁▁▁"


def test_sparkline_rising():
    out = sparkline([0, 5, 10])
    assert len(out) == 3
    assert out[0] == "▁"
    assert out[-1] == "█"


def test_sparkline_empty():
    assert sparkline([]) == ""


def test_help_text_lists_all_commands_and_keys():
    text = help_text()
    for name in COMMAND_NAMES:
        assert name in text
    for key in ("ctrl+p", "F1", "ctrl+s", "ctrl+t", "tab", "up/down"):
        assert key in text


def test_history_push_strips_and_skips_empty():
    history = []
    history_push(history, "  hello  ")
    assert history == ["hello"]
    history_push(history, "   ")
    assert history == ["hello"]


def test_history_push_dedupes_consecutive():
    history = []
    history_push(history, "a")
    history_push(history, "a")
    assert history == ["a"]


def test_history_push_caps_at_limit():
    history = []
    for i in range(60):
        history_push(history, f"line {i}")
    assert len(history) == 50
    assert history[0] == "line 10"
    assert history[-1] == "line 59"


def test_history_browse_empty_is_noop():
    index, draft, value = history_browse([], -1, "", "draft", -1)
    assert (index, draft, value) == (-1, "", None)


def test_history_browse_up_enters_and_cycles():
    history = ["one", "two", "three"]
    index, draft, value = history_browse(history, -1, "", "draft", -1)
    assert (index, draft, value) == (2, "draft", "three")
    index, draft, value = history_browse(history, index, draft, value, -1)
    assert (index, draft, value) == (1, "draft", "two")
    index, draft, value = history_browse(history, index, draft, value, -1)
    assert (index, draft, value) == (0, "draft", "one")
    index, draft, value = history_browse(history, index, draft, value, -1)
    assert (index, draft, value) == (0, "draft", None)


def test_history_browse_down_returns_newer_and_restores_draft():
    history = ["one", "two"]
    index, draft, value = history_browse(history, -1, "", "draft", -1)
    assert (index, draft, value) == (1, "draft", "two")
    index, draft, value = history_browse(history, index, draft, value, -1)
    assert (index, draft, value) == (0, "draft", "one")
    index, draft, value = history_browse(history, index, draft, value, 1)
    assert (index, draft, value) == (1, "draft", "two")
    index, draft, value = history_browse(history, index, draft, value, 1)
    assert (index, draft, value) == (-1, "draft", "draft")


def test_history_browse_down_from_fresh_is_noop():
    history = ["one"]
    index, draft, value = history_browse(history, -1, "", "draft", 1)
    assert (index, draft, value) == (-1, "", None)
