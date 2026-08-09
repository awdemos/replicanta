"""MUD mode: the deterministic half (rooms, inventory, locked gate, the
win) plus the decision chain (prompt -> parse -> wanderer fallback).
No LLM and no network: generate is always injected."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import random

import mud
from mud import MudGame


def _at(room="cave mouth"):
    game = MudGame()
    game.room = room
    return game


# -- the world -----------------------------------------------------------------

def test_look_describes_room_and_exits():
    game = MudGame()
    out = game.look()
    assert "A mossy clearing" in out
    assert "Exits: north." in out


def test_look_lists_items_present():
    out = _at().look()
    assert "You see: torch." in out


def test_go_moves_between_rooms():
    game = MudGame()
    game.act("go north")
    assert game.room == "cave mouth"


def test_bare_direction_and_alias_work():
    for cmd in ("north", "n"):
        game = MudGame()
        game.act(cmd)
        assert game.room == "cave mouth", cmd


def test_cant_go_nowhere():
    game = MudGame()
    out = game.act("go south")
    assert "can't go south from here" in out
    assert game.room == "clearing"


def test_locked_gate_blocks_without_key():
    game = _at("dark hall")
    out = game.act("go north")
    assert "locked tight" in out
    assert game.room == "dark hall"


def test_locked_gate_opens_with_key():
    game = _at("dark hall")
    game.inventory.append("brass key")
    game.act("go north")
    assert game.room == "treasury"


def test_take_places_item_in_inventory():
    game = _at()
    out = game.act("take torch")
    assert out == "You take the torch."
    assert game.inventory == ["torch"]
    assert game.rooms["cave mouth"]["items"] == []


def test_take_partial_and_filled_names():
    game = _at()
    assert game.act("take the tor") == "You take the torch."
    game = _at()
    assert game.act("grab torch") == "You take the torch."


def test_take_missing_item():
    out = _at().act("take sword")
    assert out == "There is no sword here."


def test_inventory_report():
    game = MudGame()
    assert game.act("inventory") == "You carry nothing."
    game.inventory.append("torch")
    assert game.act("inv") == "You carry: torch."


def test_look_command_matches_look():
    game = _at()
    assert game.act("look") == game.look()


def test_nonsense_command_is_ignored():
    out = _at().act("sing a song")
    assert "The dungeon ignores that." in out


def test_empty_command():
    assert MudGame().act("the a an to") == "Nothing happens."


def test_each_action_counts_as_a_turn():
    game = MudGame()
    game.act("go north")
    game.act("take torch")
    assert game.turns == 2


def test_rooms_are_copied_per_game():
    one, two = MudGame(), MudGame()
    one.act("go north")
    one.act("take torch")
    assert two.rooms["cave mouth"]["items"] == ["torch"]
    assert two.inventory == []


def test_full_walkthrough_wins():
    game = MudGame()
    plan = ["go north", "take torch", "go east", "go down",
            "take brass key", "go up", "go north", "take amulet"]
    for cmd in plan:
        out = game.act(cmd)
    assert game.won and game.finished
    assert "you have won, in 8 turns" in out


# -- parse ---------------------------------------------------------------------

def test_parse_clean_command():
    assert mud.parse_action("go north") == "go north"


def test_parse_strips_fillers_and_punctuation():
    assert mud.parse_action("Take the torch.") == "take torch"


def test_parse_accepts_bare_alias():
    assert mud.parse_action("n") == "n"


def test_parse_tolerates_prompt_prefix_and_quotes():
    assert mud.parse_action('> "go east"') == "go east"


def test_parse_rejects_prose():
    assert mud.parse_action("The torch is warm and comforting.") is None


def test_parse_rejects_empty():
    assert mud.parse_action("") is None
    assert mud.parse_action("   \n  ") is None


# -- fallback + choose ---------------------------------------------------------

def test_fallback_takes_what_is_here():
    assert mud.fallback_action(_at(), random.Random(0)) == "take torch"


def test_fallback_wanders_elsewhere():
    game = MudGame()          # clearing: only exit is north
    assert mud.fallback_action(game, random.Random(0)) == "go north"


def test_choose_uses_generated_command():
    game = MudGame()
    cmd = mud.choose_action(game, generate=lambda p: "go north")
    assert cmd == "go north"


def test_choose_falls_back_on_nonsense():
    game = MudGame()
    cmd = mud.choose_action(game, rng=random.Random(0),
                            generate=lambda p: "purple elephants")
    assert cmd == "go north"      # wanderer in the clearing


def test_choose_falls_back_on_exception():
    game = MudGame()

    def boom(prompt):
        raise RuntimeError("voice offline")

    assert mud.choose_action(game, generate=boom) == "go north"


def test_choose_passes_hint_into_prompt():
    game = MudGame()
    seen = {}

    def spy(prompt):
        seen["prompt"] = prompt
        return "go north"

    mud.choose_action(game, hint="go east!", generate=spy)
    assert "go east!" in seen["prompt"]
