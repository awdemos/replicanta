"""doom.command extraction from model replies.

The LLM writes its game move as a doom.command(...) line; the extractor
must survive the sloppy variants a small model actually produces —
trailing punctuation, prose after the call, fire-as-shootonym — while
still refusing to invent commands that are not in the valid set.
"""

from replicanta.tui_controllers import extract_doom_command


def test_exact_command_lines():
    assert extract_doom_command('doom.command("shoot")') == "shoot"
    assert extract_doom_command("doom.command('w')") == "w"
    assert extract_doom_command('doom.command( "e" )') == "e"


def test_trailing_punctuation_and_prose_are_tolerated():
    # the model loves a period or an em-dash after the call — these used to
    # silently drop the move (the "shoot always fails" symptom)
    assert extract_doom_command('doom.command("shoot").') == "shoot"
    assert extract_doom_command('doom.command("shoot")!') == "shoot"
    assert extract_doom_command('doom.command("shoot") -- take the shot') == "shoot"
    assert extract_doom_command('thinking…\ndoom.command("shoot")') == "shoot"


def test_command_call_embedded_in_prose_line():
    # small models often refuse their own format and append the call to a
    # prose line: 'The command is: doom.command("shoot")'
    assert extract_doom_command('The command is: doom.command("shoot")') == "shoot"
    assert extract_doom_command('so I will dodge left. doom.command("q") done') == "q"


def test_fire_is_a_valid_shoot_synonym():
    # the engine accepts "fire" but the extractor used to drop it
    assert extract_doom_command('doom.command("fire")') == "fire"


def test_bare_move_word_line_as_fallback():
    assert extract_doom_command("shoot") == "shoot"
    assert extract_doom_command("shoot.") == "shoot"
    assert extract_doom_command("w") == "w"


def test_non_command_lines_are_ignored():
    # markdown bold or inline mentions must not become moves
    assert extract_doom_command("**Command:** shoot") is None
    assert extract_doom_command('I will "shoot" at it later') is None
    assert extract_doom_command("The shoot would miss right now") is None


def test_invalid_or_malformed_commands_are_rejected():
    assert extract_doom_command('doom.command("teleport")') is None
    # a doubled close-paren is sloppy but the inner command is still real —
    # leniency here is deliberate
    assert extract_doom_command('doom.command("w"))') == "w"
    assert extract_doom_command("") is None
    assert extract_doom_command(None) is None
