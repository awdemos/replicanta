"""MUD mode: the deterministic half (rooms, inventory, locked gate, the
win) plus the decision chain (prompt -> parse -> wanderer fallback).
No LLM and no network: generate is always injected."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import random

import mud
from mud import MudGame, Room


def _at(room="cave mouth"):
    game = MudGame()
    game.room = room
    return game


def _org(name="Testling", user="Tester"):
    """Minimal organism-like stub for premise/scenario tests."""
    beliefs = [("self", "name", name), ("user", "name", user)]
    return SimpleNamespace(
        store=SimpleNamespace(
            beliefs=lambda: beliefs,
            belief_value=lambda obj, attr, default=None: next(
                (v for (o, _a, v) in beliefs if (o, _a) == (obj, attr)),
                default),
        ),
        dir_path=Path("testling"),
    )


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
    assert game.rooms["cave mouth"].items == []


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
    assert two.rooms["cave mouth"].items == ["torch"]
    assert two.inventory == []


def test_full_walkthrough_wins():
    game = MudGame()
    plan = ["go north", "take torch", "go east", "go down",
            "take brass key", "go up", "go north", "take amulet"]
    for cmd in plan:
        out = game.act(cmd)
    assert game.won and game.finished
    assert "you have won, in 8 turns" in out


def test_default_scenario_walkthrough():
    """The new Scenario dataclass path still supports the classic 8-turn win."""
    game = MudGame(mud.default_scenario())
    plan = ["go north", "take torch", "go east", "go down",
            "take brass key", "go up", "go north", "take amulet"]
    for cmd in plan:
        game.act(cmd)
    assert game.won and game.finished
    assert game.turns == 8


# -- act_event / session -------------------------------------------------------

def test_act_event_returns_turn_result():
    game = MudGame()
    result = game.act_event("go north", actor="user")
    assert isinstance(result, mud.TurnResult)
    assert result.moved
    assert result.text == game.look()
    assert game.session.command_log == [("user", "go north", 1)]
    assert "clearing" in game.session.visited
    assert "cave mouth" in game.session.visited


def test_plot_trigger_recorded():
    scenario = mud.default_scenario()
    scenario.rooms["cave mouth"].plot_trigger = "A bat flutters past."
    game = MudGame(scenario)
    game.act_event("go north")
    assert "A bat flutters past." in game.session.plot_beats


def test_mud_session_json_roundtrip():
    session = mud.MudSession(
        scenario_id="test",
        scenario_title="Test",
        premise="A test.",
    )
    session.visited = {"a", "b"}
    session.known_exits = {"a": {"north"}, "b": {"south", "east"}}
    session.plot_beats = ["twist"]
    session.inventory_log = [("torch", 2)]
    session.command_log = [("organism", "go north", 1)]
    session.outcome = "won"
    recovered = mud.MudSession.from_json(session.to_json())
    assert recovered.scenario_id == "test"
    assert recovered.visited == {"a", "b"}
    assert recovered.known_exits == {"a": {"north"}, "b": {"south", "east"}}
    assert recovered.plot_beats == ["twist"]
    assert recovered.inventory_log == [("torch", 2)]
    assert recovered.command_log == [("organism", "go north", 1)]
    assert recovered.outcome == "won"


# -- rendering -----------------------------------------------------------------

def test_render_map():
    game = MudGame()
    game.act("go north")
    text = mud.render_map(game)
    assert text.startswith("Known rooms (2): cave mouth, clearing")
    assert "You are in: cave mouth" in text
    assert "Exits seen from here: east, south" in text


def test_render_story():
    scenario = mud.default_scenario()
    scenario.rooms["cave mouth"].plot_trigger = "The air grows cold."
    game = MudGame(scenario)
    game.act("go north")
    text = mud.render_story(game)
    assert game.scenario.premise in text
    assert "The air grows cold." in text


def test_render_quest():
    game = MudGame()
    text = mud.render_quest(game)
    assert "Quest: The Amulet of Vatox" in text
    assert "Find the amulet." in text
    assert game.scenario.premise in text


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


def test_parse_player_command_bare_directions():
    assert mud.parse_player_command("north") == "north"
    assert mud.parse_player_command("n") == "n"


def test_parse_player_command_natural_prefixes():
    assert mud.parse_player_command("go north") == "go north"
    assert mud.parse_player_command("take the torch") == "take torch"
    assert mud.parse_player_command("I want to go east") == "go east"
    assert mud.parse_player_command("Please pick up the key") == "take key"


def test_parse_player_command_rejects_prose():
    assert mud.parse_player_command("The torch is warm.") is None


# -- premise + prompt ----------------------------------------------------------

def test_build_premise_names_organism_and_user():
    premise = mud.build_premise(_org(name="Glip", user="Ada"))
    assert "Glip" in premise
    assert "Ada" in premise
    assert "The Amulet of Vatox" in premise


def test_action_prompt_includes_context():
    game = MudGame()
    game.act("go north")
    prompt = mud.action_prompt(game, org=_org(), hint="go east!")
    assert game.scenario.premise in prompt
    assert "Known map:" in prompt
    assert "Recent moves:" in prompt
    assert "go east!" in prompt
    assert "Testling" in prompt
    assert "Tester" in prompt


# -- fallback + choose ---------------------------------------------------------

def test_fallback_prefers_unvisited_exits():
    game = _at()
    assert mud.fallback_action(game, random.Random(0)) == "go east"


def test_fallback_takes_what_is_here():
    game = _at()
    game.session.visited.add("dark hall")  # east exit no longer unvisited
    assert mud.fallback_action(game, random.Random(0)) == "take torch"


def test_fallback_wanders_elsewhere():
    game = MudGame()          # clearing: only exit is north
    assert mud.fallback_action(game, random.Random(0)) == "go north"


def test_choose_uses_generated_command():
    game = MudGame()
    cmd, _reason = mud.choose_action(game, generate=lambda p: "go north")
    assert cmd == "go north"


def test_choose_falls_back_on_nonsense():
    game = MudGame()
    cmd, _reason = mud.choose_action(game, rng=random.Random(0),
                                     generate=lambda p: "purple elephants")
    assert cmd == "go north"      # wanderer in the clearing


def test_choose_falls_back_on_exception():
    game = MudGame()

    def boom(prompt):
        raise RuntimeError("voice offline")

    assert mud.choose_action(game, generate=boom)[0] == "go north"


def test_choose_passes_hint_into_prompt():
    game = MudGame()
    seen = {}

    def spy(prompt):
        seen["prompt"] = prompt
        return "go north"

    mud.choose_action(game, hint="go east!", generate=spy)
    assert "go east!" in seen["prompt"]


# -- reasons ---------------------------------------------------------------------

def test_parse_action_with_reason_splits_chatter():
    raw = "because the torch glows like a promise\ngo north"
    assert mud.parse_action_with_reason(raw) == (
        "go north", "because the torch glows like a promise")


def test_parse_action_with_reason_none_when_only_command():
    assert mud.parse_action_with_reason("go north") == ("go north", None)


def test_choose_captures_stated_reason():
    game = MudGame()
    cmd, reason = mud.choose_action(
        game, generate=lambda p: "because the dark hall pulls at me\ngo north")
    assert cmd == "go north"
    assert reason == "because the dark hall pulls at me"


def test_choose_reason_none_when_voice_gives_only_command():
    game = MudGame()
    _cmd, reason = mud.choose_action(game, generate=lambda p: "go north")
    assert reason is None


def test_choose_fallback_reason_is_honest():
    game = MudGame()
    cmd, reason = mud.choose_action(game, rng=random.Random(0),
                                    generate=lambda p: "purple elephants")
    assert cmd == "go north"
    assert "silent" in reason


def test_choose_reason_scrubs_prompt_echoes():
    game = MudGame()
    raw = ("Draft a candidate answer, following the task instruction "
           "above exactly.\n"
           "because the key glints\ngo down")
    cmd, reason = mud.choose_action(game, generate=lambda p: raw)
    assert cmd == "go down"
    assert reason == "because the key glints"


# -- scenario generation -------------------------------------------------------

def test_validate_scenario():
    data = {
        "title": "The Tiny Tower",
        "premise": "Climb to the top.",
        "start_room": "foyer",
        "win_condition": {"item": "crown"},
        "rooms": {
            "foyer": {
                "desc": "A small foyer.",
                "exits": {"up": "tower"},
                "items": [],
            },
            "tower": {
                "desc": "The top.",
                "exits": {"down": "foyer"},
                "items": ["crown"],
            },
        },
    }
    scenario = mud.scenario_or_default(data)
    assert scenario.title == "The Tiny Tower"
    assert scenario.rooms["foyer"].exits["up"] == "tower"


def test_validate_scenario_falls_back_on_bad_exits():
    data = {
        "title": "Broken",
        "premise": "Nope.",
        "start_room": "foyer",
        "win_condition": {"item": "crown"},
        "rooms": {
            "foyer": {
                "desc": "A foyer.",
                "exits": {"up": "nowhere"},
                "items": ["crown"],
            },
        },
    }
    scenario = mud.scenario_or_default(data)
    assert scenario.title == "The Amulet of Vatox"


def test_generate_scenario_uses_default_on_bad_json():
    scenario = mud.generate_scenario("haunted space station", _org(),
                                     generate=lambda p: "not json")
    assert scenario.title == "The Amulet of Vatox"


def test_generate_scenario_uses_default_on_exception():
    def boom(prompt):
        raise RuntimeError("ollama down")

    scenario = mud.generate_scenario("haunted space station", _org(), generate=boom)
    assert scenario.title == "The Amulet of Vatox"


def test_generate_scenario_parses_valid_json():
    data = {
        "title": "The Tiny Tower",
        "premise": "Climb to the top.",
        "start_room": "foyer",
        "win_condition": {"item": "crown"},
        "rooms": {
            "foyer": {
                "desc": "A small foyer.",
                "exits": {"up": "tower"},
                "items": [],
            },
            "tower": {
                "desc": "The top.",
                "exits": {"down": "foyer"},
                "items": ["crown"],
            },
        },
    }
    scenario = mud.generate_scenario("tower", _org(),
                                     generate=lambda p: __import__("json").dumps(data))
    assert scenario.title == "The Tiny Tower"


def test_scenario_json_roundtrip():
    """Generated scenarios can be saved to disk and loaded back."""
    import json

    scenario = mud.default_scenario()
    data = mud.scenario_to_json(scenario)
    blob = json.dumps(data)  # must be JSON-safe
    recovered = mud.scenario_from_json(json.loads(blob))
    assert recovered.title == scenario.title
    assert recovered.premise == scenario.premise
    assert recovered.start_room == scenario.start_room
    assert recovered.win_condition == scenario.win_condition
    assert set(recovered.rooms) == set(scenario.rooms)
    assert recovered.rooms["dark hall"].locked["north"] == (
        "brass key", "The rusty gate is locked tight.")


def test_loaded_scenario_is_playable(tmp_path):
    """A scenario saved and reloaded still supports the classic win path."""
    import json

    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(mud.scenario_to_json(mud.default_scenario())))
    scenario = mud.scenario_from_json(json.loads(path.read_text()))
    game = MudGame(scenario)
    plan = ["go north", "take torch", "go east", "go down",
            "take brass key", "go up", "go north", "take amulet"]
    for cmd in plan:
        game.act(cmd)
    assert game.won and game.finished
    assert game.turns == 8


# -- win conditions ------------------------------------------------------------

def test_room_based_win_condition():
    scenario = mud.Scenario(
        title="Reach the Goal",
        premise="Walk to the goal.",
        start_room="start",
        rooms={
            "start": Room(desc="Start.", exits={"north": "goal"}),
            "goal": Room(desc="Goal.", exits={"south": "start"}, is_goal=True),
        },
        win_condition={"room": "goal"},
    )
    game = MudGame(scenario)
    result = game.act_event("go north")
    assert result.finished and result.won
    assert "won" in result.text
