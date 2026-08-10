"""MUD mode: the organism plays a tiny text adventure in the chat window.

/mud toggles it. The world is deterministic (rooms, exits, items, one
locked gate); the player is the organism's voice — a compact prompt
describes the room and the legal commands, the model replies with one
command, and a forgiving parser executes it. When the voice is offline
or answers nonsense, a random wanderer steps in so the game never
stalls. Moves need little brains: REPLICANTA_MUD_MODEL defaults to a
small fast model, REPLICANTA_MUD_TIMEOUT caps each decision.

Deliberate style island: this module keeps type annotations (the rest
of the codebase is unannotated) because scenario JSON crosses a
parsing boundary where the shapes earn their keep.
"""

import fileutil
import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import TypedDict

import llmclient

logger = logging.getLogger(__name__)


def _mud_model():
    """Decision model (env: REPLICANTA_MUD_MODEL, read per call)."""
    return os.environ.get("REPLICANTA_MUD_MODEL", "qwen2.5:3b")


def _mud_timeout():
    """Per-decision timeout seconds (env: REPLICANTA_MUD_TIMEOUT, per call)."""
    return int(os.environ.get("REPLICANTA_MUD_TIMEOUT", "60"))

DIR_ALIASES = {"n": "north", "s": "south", "e": "east", "w": "west",
               "u": "up", "d": "down"}
_ALL_DIRECTIONS = set(DIR_ALIASES) | set(DIR_ALIASES.values())

_FILLER = ("the", "a", "an", "to")


@dataclass
class Room:
    desc: str
    exits: dict[str, str] = field(default_factory=dict)
    items: list[str] = field(default_factory=list)
    locked: dict[str, tuple[str, str]] = field(default_factory=dict)
    plot_trigger: str | None = None
    is_goal: bool = False


class WinCondition(TypedDict, total=False):
    """How a scenario is won: take 'item', or reach 'room'. 'win_text'
    optionally overrides the flavor line spoken when the item is taken
    (scenario data, so generated scenarios can narrate their own win)."""
    item: str
    room: str
    win_text: str


@dataclass
class Scenario:
    title: str
    premise: str
    start_room: str
    rooms: dict[str, Room]
    win_condition: WinCondition = field(default_factory=dict)


@dataclass
class TurnResult:
    text: str
    moved: bool = False
    took: str | None = None
    plot: str | None = None
    finished: bool = False
    won: bool = False


@dataclass
class MudSession:
    scenario_id: str
    scenario_title: str
    premise: str
    visited: set[str] = field(default_factory=set)
    known_exits: dict[str, set[str]] = field(default_factory=dict)
    plot_beats: list[str] = field(default_factory=list)
    inventory_log: list[tuple[str, int]] = field(default_factory=list)
    command_log: list[tuple[str, str, int]] = field(default_factory=list)
    outcome: str | None = None

    def to_json(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "scenario_title": self.scenario_title,
            "premise": self.premise,
            "visited": sorted(self.visited),
            "known_exits": {k: sorted(v) for k, v in self.known_exits.items()},
            "plot_beats": list(self.plot_beats),
            "inventory_log": [list(t) for t in self.inventory_log],
            "command_log": [[a, c, t] for a, c, t in self.command_log],
            "outcome": self.outcome,
        }

    @classmethod
    def from_json(cls, data: dict) -> "MudSession":
        return cls(
            scenario_id=data["scenario_id"],
            scenario_title=data["scenario_title"],
            premise=data["premise"],
            visited=set(data.get("visited", [])),
            known_exits={k: set(v) for k, v in data.get("known_exits", {}).items()},
            plot_beats=list(data.get("plot_beats", [])),
            inventory_log=[tuple(t) for t in data.get("inventory_log", [])],
            command_log=[tuple(t) for t in data.get("command_log", [])],
            outcome=data.get("outcome"),
        )


def default_scenario() -> Scenario:
    """The built-in deterministic dungeon: mossy clearing, cave, locked gate."""
    return Scenario(
        title="The Amulet of Vatox",
        premise=("A mossy clearing, a cave mouth, and a locked treasury. "
                 "Find the amulet and escape."),
        start_room="clearing",
        rooms={
            "clearing": Room(
                desc=("A mossy clearing under a flat grey sky. "
                      "A cave yawns to the north."),
                exits={"north": "cave mouth"},
                items=[],
            ),
            "cave mouth": Room(
                desc=("The cave's mouth. Cold air breathes out of the dark. "
                      "A torch leans against the rock."),
                exits={"south": "clearing", "east": "dark hall"},
                items=["torch"],
            ),
            "dark hall": Room(
                desc=("A long hall of wet stone. Steps spiral down; "
                      "a rusty gate blocks the north arch."),
                exits={"west": "cave mouth", "down": "well room",
                       "north": "treasury"},
                locked={"north": ("brass key", "The rusty gate is locked tight.")},
                items=[],
            ),
            "well room": Room(
                desc="A round room around an old well. Something glints on the rim.",
                exits={"up": "dark hall"},
                items=["brass key"],
            ),
            "treasury": Room(
                desc=("A vault glittering with old coins. "
                      "On a pedestal: the amulet."),
                exits={"south": "dark hall"},
                items=["amulet"],
                is_goal=True,
            ),
        },
        win_condition={"item": "amulet",
                           "win_text": ("You lift the amulet. "
                                        "The dungeon exhales")},
    )


class MudGame:
    """The deterministic half of the game: rooms, inventory, the locked
    gate, the win. Pure — rendering and model calls are the caller's."""

    def __init__(self, scenario=None, session=None):
        self.scenario = scenario or default_scenario()
        self.session = session or MudSession(
            scenario_id=fileutil.slug(self.scenario.title),
            scenario_title=self.scenario.title,
            premise=self.scenario.premise,
        )
        # Per-game copies: taking an item must not drain the world template.
        self.rooms = {
            name: Room(
                desc=room.desc,
                exits=dict(room.exits),
                items=list(room.items),
                locked={k: tuple(v) for k, v in room.locked.items()},
                plot_trigger=room.plot_trigger,
                is_goal=room.is_goal,
            )
            for name, room in self.scenario.rooms.items()
        }
        self.room = self.scenario.start_room
        self._record_room(self.room)
        self.inventory = []
        self.turns = 0
        self.finished = False
        self.won = False

    def look(self):
        room = self.rooms[self.room]
        bits = [room.desc]
        if room.items:
            bits.append("You see: " + ", ".join(room.items) + ".")
        bits.append("Exits: " + ", ".join(sorted(room.exits)) + ".")
        return " ".join(bits)

    def act(self, command):
        """One command -> what happened (text). Forgiving parser: exit
        names and single letters work as bare directions, fillers like
        'the' are ignored."""
        return self.act_event(command).text

    def act_event(self, command, actor="organism") -> TurnResult:
        """Execute one command and return a structured TurnResult."""
        self.turns += 1
        self.session.command_log.append((actor, command.strip(), self.turns))
        self._record_room(self.room)

        words = [w for w in command.strip().lower().split()
                 if w not in _FILLER]
        if not words:
            return TurnResult(text="Nothing happens.")

        verb = words[0]
        if verb in ("go", "move", "walk") and len(words) > 1:
            result = self._go(words[1])
        elif verb in DIR_ALIASES or verb in self.rooms[self.room].exits:
            result = self._go(verb)
        elif verb in ("take", "get", "grab") and len(words) > 1:
            result = self._take(" ".join(words[1:]))
        elif verb == "look":
            result = TurnResult(text=self.look())
        elif verb in ("inventory", "inv", "i"):
            result = TurnResult(text=(
                "You carry: " + ", ".join(self.inventory) + "."
                if self.inventory else "You carry nothing."))
        else:
            result = TurnResult(
                text=f"'{command.strip()}'? The dungeon ignores that.")

        if result.moved:
            self._record_room(self.room)
            new_room = self.rooms[self.room]
            if (new_room.plot_trigger
                    and new_room.plot_trigger not in self.session.plot_beats):
                result.plot = new_room.plot_trigger
                self.session.plot_beats.append(new_room.plot_trigger)

        if result.finished:
            self.finished = True
            self.won = result.won
            self.session.outcome = "won" if result.won else "lost"

        return result

    def _record_room(self, room_id):
        self.session.visited.add(room_id)
        room = self.rooms[room_id]
        self.session.known_exits.setdefault(room_id, set()).update(room.exits.keys())

    def _go(self, direction):
        direction = DIR_ALIASES.get(direction, direction)
        room = self.rooms[self.room]
        if direction not in room.exits:
            return TurnResult(text=f"You can't go {direction} from here.")
        locked = room.locked.get(direction)
        if locked:
            key, message = locked
            if key not in self.inventory:
                return TurnResult(text=message)
        self.room = room.exits[direction]
        text = self.look()
        finished = False
        won = False
        if self.scenario.win_condition.get("room") == self.room:
            text += f" You have reached your destination and won, in {self.turns} turns."
            finished = True
            won = True
        return TurnResult(text=text, moved=True, finished=finished, won=won)

    def _take(self, item):
        room = self.rooms[self.room]
        for held in list(room.items):
            if item in held or held in item:
                room.items.remove(held)
                self.inventory.append(held)
                self.session.inventory_log.append((held, self.turns))
                if held == self.scenario.win_condition.get("item"):
                    custom = self.scenario.win_condition.get("win_text")
                    if custom:
                        text = (f"{custom} — "
                                f"you have won, in {self.turns} turns.")
                    else:
                        text = (f"You take the {held}. The dungeon exhales — "
                                f"you have won, in {self.turns} turns.")
                    return TurnResult(text=text, took=held,
                                      finished=True, won=True)
                if self.scenario.win_condition.get("room") == self.room:
                    return TurnResult(text=f"You take the {held}. "
                                           "The quest is complete!",
                                      took=held, finished=True, won=True)
                return TurnResult(text=f"You take the {held}.", took=held)
        return TurnResult(text=f"There is no {item} here.")


# -- rendering -----------------------------------------------------------------

def render_map(game) -> str:
    """Text map of rooms the organism has discovered."""
    known = sorted(game.session.visited)
    lines = [f"Known rooms ({len(known)}): {', '.join(known)}"]
    lines.append(f"You are in: {game.room}")
    room = game.rooms[game.room]
    labels = []
    for direction in sorted(room.exits):
        label = direction
        if direction in room.locked:
            label += " (locked)"
        labels.append(label)
    lines.append(f"Exits seen from here: {', '.join(labels)}")
    return "\n".join(lines)


def render_story(game) -> str:
    """Premise and plot triggers seen so far."""
    lines = [game.session.premise or game.scenario.premise]
    if game.session.plot_beats:
        lines.append("")
        lines.append("Plot so far:")
        for beat in game.session.plot_beats:
            lines.append(f"- {beat}")
    return "\n".join(lines)


def render_quest(game) -> str:
    """Current quest and win condition."""
    scenario = game.scenario
    condition = scenario.win_condition
    if "item" in condition:
        goal = f"Find the {condition['item']}."
    elif "room" in condition:
        goal = f"Reach the {condition['room']}."
    else:
        goal = "Complete the quest."
    return "\n".join([
        f"Quest: {scenario.title}",
        scenario.premise,
        "",
        f"Goal: {goal}",
    ])


# -- the player ----------------------------------------------------------------

def _org_name(org):
    """Best-effort organism name from beliefs or directory."""
    if org is None:
        return "the organism"
    name = org.store.belief_value("self", "name")
    if name:
        return name
    dir_path = getattr(org, "dir_path", None)
    if dir_path is not None and getattr(dir_path, "name", None):
        return dir_path.name
    return "the organism"


def _user_name(org):
    """Best-effort user name from beliefs."""
    if org is None:
        return "the user"
    return org.store.belief_value("user", "name", "the user")


def build_premise(org, scenario=None) -> str:
    """Opening premise that names the organism and the user."""
    scenario = scenario or default_scenario()
    return (f"You are {_org_name(org)}, a small mind that lives in a "
            f"terminal. {_user_name(org)} sits beyond the screen, watching. "
            f"Together you have entered {scenario.title}: {scenario.premise}")


def action_prompt(game, org=None, hint=None):
    """Compact decision prompt: room, map, story, inventory, legal commands,
    and an optional one-shot nudge shouted by the user."""
    lines = [
        ("You are playing a tiny text adventure. First write one short "
         "sentence about why you choose your move, starting with "
         "'because'. Then, on a new line, write exactly one command "
         "and nothing else."),
    ]
    if org is not None:
        lines.append(f"You are {_org_name(org)}. {_user_name(org)} is watching "
                     "from beyond the screen.")
    lines.extend([
        f"Current quest: {game.scenario.premise}",
        f"Scenario: {game.scenario.title}",
        f"Room: {game.look()}",
        f"Inventory: {', '.join(game.inventory) or 'empty'}",
        "Known map:",
        render_map(game),
    ])
    if game.session.plot_beats:
        lines.extend([
            "Story so far:",
            "\n".join(f"- {beat}" for beat in game.session.plot_beats),
        ])
    recent = game.session.command_log[-5:]
    if recent:
        lines.append("Recent moves:")
        for actor, cmd, turn in recent:
            lines.append(f"- turn {turn} ({actor}): {cmd}")
    lines.append(
        "Commands: go <exit> (or just the exit name), take <item>, look, "
        "inventory.")
    if hint:
        lines.append(f"A friend watching shouts: {hint}")
    lines.append("Your move:")
    return "\n".join(lines)


_COMMAND_STARTERS = ("go", "take", "get", "grab", "look", "move",
                     "walk", "inventory", "inv")


def _command_words(line):
    """Normalized command words for a line, or None when the line is
    not a command."""
    line = line.lower().lstrip(">").strip().strip('"').rstrip(".")
    words = [w for w in line.split() if w not in _FILLER]
    if not words:
        return None
    if words[0] in _COMMAND_STARTERS or words[0] in DIR_ALIASES:
        return words
    return None


def parse_action(text):
    """Model output -> one command string, or None when unusable."""
    return parse_action_with_reason(text)[0]


def parse_action_with_reason(text):
    """Model output -> (command, reason): the first command-like line
    normalized, plus whatever else the organism said as its stated
    reason (None when it offered nothing but the command)."""
    if not text:
        return None, None
    command = None
    reason_lines = []
    for line in text.strip().splitlines():
        words = _command_words(line.strip())
        if command is None and words is not None:
            command = " ".join(words)
        elif line.strip():
            reason_lines.append(line.strip())
    reason = " ".join(reason_lines).strip() or None
    return command, reason


def parse_player_command(text):
    """User chat input -> normalized command string, or None if not a command."""
    if not text:
        return None
    text = text.strip().lower().rstrip(".!?")
    for prefix in ("i want to ", "i would like to ", "can i ", "please ",
                   "i "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    words = [w for w in text.split() if w not in _FILLER]
    if not words:
        return None

    # Bare direction or alias.
    if words[0] in _ALL_DIRECTIONS:
        return words[0]

    # Take / get / grab / pick up.
    if words[0] in ("take", "get", "grab") and len(words) > 1:
        return "take " + " ".join(words[1:])
    if words[0] == "pick" and len(words) > 2 and words[1] == "up":
        return "take " + " ".join(words[2:])

    # Go / move / walk.
    if words[0] in ("go", "move", "walk") and len(words) > 1:
        return "go " + words[1]

    # Look and inventory.
    if words[0] == "look":
        return "look"
    if words[0] in ("inventory", "inv", "i"):
        return "inventory"

    return None


def fallback_action(game, rng):
    """The wanderer: prefer unvisited exits, then items, then any exit."""
    room = game.rooms[game.room]
    unvisited = [
        direction for direction in room.exits
        if room.exits[direction] not in game.session.visited
    ]
    if unvisited:
        return "go " + rng.choice(sorted(unvisited))
    if room.items:
        return "take " + room.items[0]
    return "go " + rng.choice(sorted(room.exits))


def choose_action(game, hint=None, rng=None, generate=None, org=None):
    """The organism's next move: ask the voice, parse it, fall back to
    the wanderer when the voice is silent or speaks nonsense. Returns
    (command, reason) — the reason is the organism's stated because-line,
    or the honest fallback excuse when the wanderer chose."""
    rng = rng if rng is not None else random.Random()
    if generate is None:
        def generate(prompt):
            return llmclient.generate(
                prompt, model=_mud_model(), timeout=_mud_timeout(),
                temperature=0.7)
    command = reason = None
    try:
        raw = generate(action_prompt(game, org=org, hint=hint))
        # the voice is chatty; scrub echoed prompt scaffolding before
        # reading the move and its reason
        command, reason = parse_action_with_reason(
            llmclient.clean_candidate(raw or ""))
    except Exception:  # noqa: BLE001, S110 — a silent voice means wandering
        pass
    if command is None:
        command = fallback_action(game, rng)
        reason = "the inner voice was silent — wandering on instinct"
    return command, reason


# -- scenario generation -------------------------------------------------------

def _scenario_generation_prompt(description, org):
    org_name = _org_name(org)
    user_name = _user_name(org)
    return (
        "You are a MUD designer. Create a compact text-adventure scenario "
        "(5-8 rooms) as JSON:\n"
        "{\n"
        '  "title": "...",\n'
        '  "premise": "... (mention the organism and the user)",\n'
        '  "start_room": "room_id",\n'
        '  "win_condition": {"item": "..."},\n'
        '  "rooms": {\n'
        '    "room_id": {\n'
        '      "desc": "...",\n'
        '      "exits": {"north": "other_room_id"},\n'
        '      "items": ["..."],\n'
        '      "locked": {"east": ["key_item_id", "locked message"]},\n'
        '      "plot_trigger": "optional text on first entry"\n'
        "    }\n"
        "  }\n"
        "}\n"
        f"Setting: {description}\n"
        "Keep it winnable in 6-15 turns. Reply with only the JSON.\n"
        f"The protagonist is {org_name}; {user_name} watches from beyond the screen."
    )


def scenario_or_default(data) -> Scenario:
    """Validate and normalize scenario JSON; any error substitutes the
    default scenario (with a logged warning) rather than failing."""
    try:
        title = data["title"]
        premise = data["premise"]
        start_room = data["start_room"]
        win_condition = dict(data["win_condition"])
        rooms_data = data["rooms"]

        if start_room not in rooms_data:
            raise ValueError(f"start_room {start_room!r} not in rooms")

        rooms = {}
        for room_id, room_data in rooms_data.items():
            desc = room_data["desc"]
            exits = dict(room_data.get("exits", {}))
            items = list(room_data.get("items", []))
            locked_raw = room_data.get("locked", {})
            locked = {}
            for direction, lock_info in locked_raw.items():
                if not isinstance(lock_info, (list, tuple)) or len(lock_info) < 2:
                    raise ValueError(
                        f"invalid locked format for {direction} in {room_id}")
                locked[direction] = (lock_info[0], lock_info[1])
            plot_trigger = room_data.get("plot_trigger")
            is_goal = room_data.get("is_goal", False)
            rooms[room_id] = Room(
                desc=desc,
                exits=exits,
                items=items,
                locked=locked,
                plot_trigger=plot_trigger,
                is_goal=is_goal,
            )

        for room_id, room in rooms.items():
            for direction, target in room.exits.items():
                if target not in rooms:
                    raise ValueError(
                        f"exit {direction} from {room_id} to unknown {target}")

        if "item" in win_condition:
            item = win_condition["item"]
            if not any(item in room.items for room in rooms.values()):
                raise ValueError(f"win item {item!r} not found in any room")
        elif "room" in win_condition:
            if win_condition["room"] not in rooms:
                raise ValueError(
                    f"win room {win_condition['room']!r} not found")
        else:
            raise ValueError("win_condition must contain 'item' or 'room'")

        return Scenario(
            title=title,
            premise=premise,
            start_room=start_room,
            rooms=rooms,
            win_condition=win_condition,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("MUD scenario validation failed: %s; using default", exc)
        return default_scenario()


# -- scenario serialization ----------------------------------------------------

def scenario_to_json(scenario) -> dict:
    """Scenario -> plain JSON-safe dict (for saving generated scenarios)."""
    return {
        "title": scenario.title,
        "premise": scenario.premise,
        "start_room": scenario.start_room,
        "win_condition": dict(scenario.win_condition),
        "rooms": {
            room_id: {
                "desc": room.desc,
                "exits": dict(room.exits),
                "items": list(room.items),
                "locked": {k: list(v) for k, v in room.locked.items()},
                "plot_trigger": room.plot_trigger,
                "is_goal": room.is_goal,
            }
            for room_id, room in scenario.rooms.items()
        },
    }


def scenario_from_json(data) -> Scenario:
    """JSON dict -> Scenario, substituting the default on bad input
    (same contract as scenario_or_default)."""
    return scenario_or_default(data)


def generate_scenario(description, org, generate=None) -> Scenario:
    """Ask the voice for a scenario, validate it, and fall back on failure."""
    if generate is None:
        def generate(prompt):
            return llmclient.generate(
                prompt, model=_mud_model(), timeout=_mud_timeout(), temperature=0.7)
    prompt = _scenario_generation_prompt(description, org)
    try:
        raw = generate(prompt)
        text = raw.strip()
        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines()
                if not line.strip().startswith("```"))
            text = text.strip()
        data = json.loads(text)
        return scenario_or_default(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("MUD scenario generation failed: %s; using default", exc)
        return default_scenario()
