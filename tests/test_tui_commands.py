"""Unit tests for the slash-command registry helpers."""

from replicanta import tui_commands


def test_filter_commands_matches_name_and_description():
    assert any(c[0] == "/chaos" for c in tui_commands.filter_commands("randomness"))
    assert any(c[0] == "/voice" for c in tui_commands.filter_commands("voice"))
    assert tui_commands.filter_commands("xyzxyz") == []


def test_palette_items_returns_all_commands():
    assert tui_commands.palette_items() == tui_commands.COMMANDS


def test_every_command_has_a_category():
    for command in tui_commands.COMMANDS:
        assert len(command) == 4
        assert command[3] in {"State", "Voice", "Senses", "MUD", "Organisms", "System", "Help"}
