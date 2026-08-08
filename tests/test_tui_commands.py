import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tui_commands import COMMAND_NAMES, complete_command, help_text, sparkline


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
    for key in ("ctrl+p", "F1", "ctrl+s", "ctrl+t", "tab"):
        assert key in text
