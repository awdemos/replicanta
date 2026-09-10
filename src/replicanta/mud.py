"""MUD mode: the organism plays a tiny text adventure in the chat window.

/mud toggles it. The world is deterministic (rooms, exits, items, one
locked gate); the player is the organism's voice — a compact prompt
describes the room and the legal commands, the model replies with one
command, and a forgiving parser executes it. When the voice is offline
or answers nonsense, a random wanderer steps in so the game never
stalls. Moves need little brains: REPLICANTA_MUD_MODEL defaults to a
small fast model, REPLICANTA_MUD_TIMEOUT caps each decision.

Multi-actor support: a MudGame can host any number of actors (organisms
or users). All actors share the same world, but each has its own room
and inventory. Turns advance round-robin through the turn order.

Deliberate style island: this module keeps type annotations (the rest
of the codebase is unannotated) because scenario JSON crosses a
parsing boundary where the shapes earn their keep.
"""

import json
import logging
import os
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple, TypedDict

from replicanta import fileutil, llmclient, voice

logger = logging.getLogger(__name__)


def _mud_model():
    """Decision model (env: REPLICANTA_MUD_MODEL, read per call)."""
    return os.environ.get("REPLICANTA_MUD_MODEL", "qwen2.5:3b")


def _mud_timeout():
    """Per-decision timeout seconds (env: REPLICANTA_MUD_TIMEOUT, per call)."""
    return int(os.environ.get("REPLICANTA_MUD_TIMEOUT", "60"))


DIR_ALIASES = {
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "u": "up",
    "d": "down",
}
_ALL_DIRECTIONS = set(DIR_ALIASES) | set(DIR_ALIASES.values())

_FILLER = ("the", "a", "an", "to")


@dataclass
class Room:
    """One location in a MUD scenario: description, exits, items, and
    optional locked gates with a key requirement."""

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


class RoomDict(TypedDict, total=False):
    """JSON shape for a single room in a scenario."""

    desc: str
    exits: dict[str, str]
    items: list[str]
    locked: dict[str, list[str]]
    plot_trigger: str
    is_goal: bool


class ScenarioDict(TypedDict):
    """JSON shape produced by scenario_to_json and accepted by validate_scenario."""

    title: str
    premise: str
    start_room: str
    win_condition: WinCondition
    rooms: dict[str, RoomDict]


class ActorStateDict(TypedDict):
    """Serialized state for one actor in a session."""

    room: str
    inventory: list[str]
    kind: str


class MudSessionDict(TypedDict, total=False):
    """JSON shape produced by MudSession.to_json and accepted by MudSession.from_json."""

    scenario_id: str
    scenario_title: str
    premise: str
    visited: list[str]
    known_exits: dict[str, list[str]]
    plot_beats: list[str]
    inventory_log: list[list[str | int]]
    command_log: list[list[str | int]]
    outcome: str | None
    actors: dict[str, ActorStateDict]
    turn_order: list[str]
    turn_index: int


class ActionChoice(NamedTuple):
    """A chosen MUD command plus the model's stated reason (if any)."""

    command: str | None
    reason: str | None


@dataclass
class Scenario:
    """A complete MUD world: title, premise, starting room, rooms, and
    the win condition that ends the quest."""

    title: str
    premise: str
    start_room: str
    rooms: dict[str, Room]
    win_condition: WinCondition = field(default_factory=dict)


@dataclass
class TurnResult:
    """Structured outcome of a single MUD command."""

    text: str
    moved: bool = False
    took: str | None = None
    plot: str | None = None
    finished: bool = False
    won: bool = False


@dataclass
class MudActor:
    """One participant in a MUD session: a name, current room, inventory,
    and kind (organism or user)."""

    name: str
    room: str
    inventory: list[str] = field(default_factory=list)
    kind: str = "organism"


@dataclass
class MudWorld:
    """The shared, mutable game world: scenario and room instances."""

    scenario: Scenario
    rooms: dict[str, Room]

    @classmethod
    def from_scenario(cls, scenario: Scenario) -> "MudWorld":
        """Create a mutable copy of the scenario's rooms."""
        rooms = {
            name: Room(
                desc=room.desc,
                exits=dict(room.exits),
                items=list(room.items),
                locked={k: tuple(v) for k, v in room.locked.items()},
                plot_trigger=room.plot_trigger,
                is_goal=room.is_goal,
            )
            for name, room in scenario.rooms.items()
        }
        return cls(scenario=scenario, rooms=rooms)

    def look(self, room_id: str) -> str:
        """Describe a room, its items, and its exits."""
        room = self.rooms[room_id]
        bits = [room.desc]
        if room.items:
            bits.append("You see: " + ", ".join(room.items) + ".")
        bits.append("Exits: " + ", ".join(sorted(room.exits)) + ".")
        return " ".join(bits)

    def go(self, actor: MudActor, direction: str, turn: int = 0) -> TurnResult:
        """Move an actor in a direction, respecting locks and win conditions."""
        direction = DIR_ALIASES.get(direction, direction)
        room = self.rooms[actor.room]
        if direction not in room.exits:
            return TurnResult(text=f"You can't go {direction} from here.")
        locked = room.locked.get(direction)
        if locked:
            key, message = locked
            if key not in actor.inventory:
                return TurnResult(text=message)
        actor.room = room.exits[direction]
        text = self.look(actor.room)
        finished = False
        won = False
        if self.scenario.win_condition.get("room") == actor.room:
            text += (
                f" You have reached your destination and won, in {turn} turns."
            )
            finished = True
            won = True
        return TurnResult(text=text, moved=True, finished=finished, won=won)

    def take(self, actor: MudActor, item: str, turn: int = 0) -> TurnResult:
        """Have an actor take an item from their current room."""
        room = self.rooms[actor.room]
        for held in list(room.items):
            if item in held or held in item:
                room.items.remove(held)
                actor.inventory.append(held)
                if held == self.scenario.win_condition.get("item"):
                    custom = self.scenario.win_condition.get("win_text")
                    if custom:
                        text = f"{custom} — you have won, in {turn} turns."
                    else:
                        text = (
                            f"You take the {held}. The dungeon exhales — "
                            f"you have won, in {turn} turns."
                        )
                    return TurnResult(text=text, took=held, finished=True, won=True)
                if self.scenario.win_condition.get("room") == actor.room:
                    return TurnResult(
                        text=f"You take the {held}. The quest is complete!",
                        took=held,
                        finished=True,
                        won=True,
                    )
                return TurnResult(text=f"You take the {held}.", took=held)
        return TurnResult(text=f"There is no {item} here.")


@dataclass
class MudSession:
    """Persistable record of a play-through: visited rooms, known exits,
    story beats, per-actor state, command log, and final outcome."""

    scenario_id: str
    scenario_title: str
    premise: str
    visited: set[str] = field(default_factory=set)
    known_exits: dict[str, set[str]] = field(default_factory=dict)
    plot_beats: list[str] = field(default_factory=list)
    inventory_log: list[tuple[str, int]] = field(default_factory=list)
    command_log: list[tuple[str, str, int]] = field(default_factory=list)
    outcome: str | None = None
    actors: dict[str, MudActor] = field(default_factory=dict)
    turn_order: list[str] = field(default_factory=list)
    turn_index: int = 0

    def to_json(self) -> MudSessionDict:
        """Serialize to the JSON-safe dict consumed by :meth:`from_json`."""
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
            "actors": {
                name: {
                    "room": actor.room,
                    "inventory": list(actor.inventory),
                    "kind": actor.kind,
                }
                for name, actor in self.actors.items()
            },
            "turn_order": list(self.turn_order),
            "turn_index": self.turn_index,
        }

    @classmethod
    def from_json(cls, data: MudSessionDict) -> "MudSession":
        """Rebuild a session from its JSON serialization.

        Old single-actor saves without an ``actors`` dict are migrated to a
        single actor named ``organism`` at the scenario's start room.
        """
        scenario_id = data["scenario_id"]
        scenario_title = data.get("scenario_title", scenario_id)
        premise = data.get("premise", "")
        visited = set(data.get("visited", []))
        known_exits = {k: set(v) for k, v in data.get("known_exits", {}).items()}
        plot_beats = list(data.get("plot_beats", []))
        inventory_log = [tuple(t) for t in data.get("inventory_log", [])]
        command_log = [tuple(t) for t in data.get("command_log", [])]
        outcome = data.get("outcome")

        actors_data = data.get("actors")
        turn_order = list(data.get("turn_order", []))
        turn_index = data.get("turn_index", 0)

        if actors_data:
            actors = {
                name: MudActor(
                    name=name,
                    room=actor_data.get("room", ""),
                    inventory=list(actor_data.get("inventory", [])),
                    kind=actor_data.get("kind", "organism"),
                )
                for name, actor_data in actors_data.items()
            }
        else:
            # Legacy single-actor save: infer the organism's room from the
            # last command, or default to the scenario start.
            room = _legacy_actor_room(command_log) or ""
            actors = {
                "organism": MudActor(
                    name="organism", room=room, inventory=[], kind="organism"
                )
            }
            turn_order = ["organism"]
            turn_index = 0

        return cls(
            scenario_id=scenario_id,
            scenario_title=scenario_title,
            premise=premise,
            visited=visited,
            known_exits=known_exits,
            plot_beats=plot_beats,
            inventory_log=inventory_log,
            command_log=command_log,
            outcome=outcome,
            actors=actors,
            turn_order=turn_order,
            turn_index=turn_index,
        )


def _legacy_actor_room(command_log: list[tuple[str, str, int]]) -> str | None:
    """Best-guess current room for a legacy single-actor save.

    Replays the logged moves against a fresh default world to find where
    the actor ended up. This is only used for migrating old saves.
    """
    if not command_log:
        return None
    world = MudWorld.from_scenario(default_scenario())
    actor = MudActor(name="organism", room=default_scenario().start_room)
    for _actor_name, command, _turn in command_log:
        words = [w for w in command.strip().lower().split() if w not in _FILLER]
        if not words:
            continue
        verb = words[0]
        if verb in ("go", "move", "walk") and len(words) > 1:
            world.go(actor, words[1])
        elif verb in DIR_ALIASES or verb in world.rooms[actor.room].exits:
            world.go(actor, verb)
        elif verb in ("take", "get", "grab") and len(words) > 1:
            world.take(actor, " ".join(words[1:]))
    return actor.room


def default_scenario() -> Scenario:
    """The built-in deterministic dungeon: mossy clearing, cave, locked gate."""
    return Scenario(
        title="The Amulet of Vatox",
        premise=(
            "A mossy clearing, a cave mouth, and a locked treasury. "
            "Find the amulet and escape."
        ),
        start_room="clearing",
        rooms={
            "clearing": Room(
                desc=(
                    "A mossy clearing under a flat grey sky. A cave yawns to the north."
                ),
                exits={"north": "cave mouth"},
                items=[],
            ),
            "cave mouth": Room(
                desc=(
                    "The cave's mouth. Cold air breathes out of the dark. "
                    "A torch leans against the rock."
                ),
                exits={"south": "clearing", "east": "dark hall"},
                items=["torch"],
            ),
            "dark hall": Room(
                desc=(
                    "A long hall of wet stone. Steps spiral down; "
                    "a rusty gate blocks the north arch."
                ),
                exits={"west": "cave mouth", "down": "well room", "north": "treasury"},
                locked={"north": ("brass key", "The rusty gate is locked tight.")},
                items=[],
            ),
            "well room": Room(
                desc="A round room around an old well. Something glints on the rim.",
                exits={"up": "dark hall"},
                items=["brass key"],
            ),
            "treasury": Room(
                desc=("A vault glittering with old coins. On a pedestal: the amulet."),
                exits={"south": "dark hall"},
                items=["amulet"],
                is_goal=True,
            ),
        },
        win_condition={
            "item": "amulet",
            "win_text": ("You lift the amulet. The dungeon exhales"),
        },
    )


class MudGame:
    """The deterministic half of the game: shared world, per-actor state,
    turn order, and the win. Pure — rendering and model calls are the caller's."""

    def __init__(self, scenario=None, session=None):
        self.world = MudWorld.from_scenario(scenario or default_scenario())
        self.session = session or MudSession(
            scenario_id=fileutil.slug(self.world.scenario.title),
            scenario_title=self.world.scenario.title,
            premise=self.world.scenario.premise,
        )
        self.actors = {}
        self.turn_order = list(self.session.turn_order)
        self.turn_index = self.session.turn_index
        # Default single-player actor for backward compatibility.
        if not self.session.actors:
            self.add_actor("organism", kind="organism", room=self.world.scenario.start_room)
        else:
            # Reattach session actors to this game instance.
            for name, actor in self.session.actors.items():
                self.actors[name] = MudActor(
                    name=actor.name,
                    room=actor.room,
                    inventory=list(actor.inventory),
                    kind=actor.kind,
                )
            if not self.turn_order:
                self.turn_order = list(self.actors.keys())
        self.paused = False
        self.turns = max((turn for _a, _c, turn in self.session.command_log), default=0)
        self.finished = self.session.outcome is not None
        self.won = self.session.outcome == "won"
        self._record_room(self.current_actor().room)

    # -- backward-compatible single-player properties ----------------------------

    @property
    def scenario(self):
        return self.world.scenario

    @property
    def rooms(self):
        return self.world.rooms

    @property
    def room(self):
        return self.current_actor().room

    @room.setter
    def room(self, value):
        self.current_actor().room = value

    @property
    def inventory(self):
        return self.current_actor().inventory

    def look(self, actor_name=None):
        """Describe the current room (or a named actor's room)."""
        actor = self.actors.get(actor_name) if actor_name else self.current_actor()
        return self.world.look(actor.room)

    # -- actor management --------------------------------------------------------

    def add_actor(
        self, name: str, kind: str = "organism", room: str | None = None
    ) -> MudActor:
        """Add a new actor to the game. Returns the actor."""
        if name in self.actors:
            return self.actors[name]
        actor = MudActor(
            name=name,
            room=room if room is not None else self.world.scenario.start_room,
            kind=kind,
        )
        self.actors[name] = actor
        self.session.actors[name] = actor
        if name not in self.turn_order:
            self.turn_order.append(name)
            self.session.turn_order = list(self.turn_order)
        return actor

    def remove_actor(self, name: str) -> bool:
        """Remove an actor from the game. Returns True if removed."""
        if name not in self.actors:
            return False
        del self.actors[name]
        self.session.actors.pop(name, None)
        if name in self.turn_order:
            idx = self.turn_order.index(name)
            self.turn_order.remove(name)
            if self.turn_index > idx:
                self.turn_index -= 1
            self.turn_index %= max(1, len(self.turn_order))
            self.session.turn_order = list(self.turn_order)
            self.session.turn_index = self.turn_index
        return True

    def current_actor(self) -> MudActor:
        """The actor whose turn it is."""
        if not self.turn_order:
            raise RuntimeError("no actors in game")
        return self.actors[self.turn_order[self.turn_index % len(self.turn_order)]]

    def current_actor_name(self) -> str:
        return self.current_actor().name

    def _advance_turn(self):
        """Move to the next actor in round-robin order."""
        if not self.turn_order or self.finished:
            return
        self.turn_index = (self.turn_index + 1) % len(self.turn_order)
        self.session.turn_index = self.turn_index

    # -- actions -----------------------------------------------------------------

    def act(self, command, actor_name=None):
        """One command -> what happened (text). Forgiving parser: exit
        names and single letters work as bare directions, fillers like
        'the' are ignored."""
        return self.act_event(command, actor_name=actor_name).text

    def act_event(self, command, actor_name=None, actor=None) -> TurnResult:
        """Execute one command and return a structured TurnResult.

        ``actor`` is a deprecated alias for ``actor_name`` kept for
        backward compatibility with the single-player API. When the named
        actor does not exist, the current actor acts and the provided name
        is used only for the command log.
        """
        provided_name = actor_name or actor
        if provided_name and provided_name in self.actors:
            actor = self.actors[provided_name]
            log_name = provided_name
        else:
            actor = self.current_actor()
            log_name = provided_name if provided_name else actor.name
        if actor is None:
            return TurnResult(text="Nothing happens.")

        self.turns += 1
        self.session.command_log.append((log_name, command.strip(), self.turns))
        self._record_room(actor.room)

        words = [w for w in command.strip().lower().split() if w not in _FILLER]
        if not words:
            result = TurnResult(text="Nothing happens.")
        else:
            result = self._act_words(actor, command, words)

        if result.moved:
            self._record_room(actor.room)
            new_room = self.world.rooms[actor.room]
            if (
                new_room.plot_trigger
                and new_room.plot_trigger not in self.session.plot_beats
            ):
                result.plot = new_room.plot_trigger
                self.session.plot_beats.append(new_room.plot_trigger)

        if result.took:
            self.session.inventory_log.append((result.took, self.turns))

        if result.finished:
            self.finished = True
            self.won = result.won
            self.session.outcome = "won" if result.won else "lost"

        # Persist actor state into the session after every action.
        self.session.actors = {name: actor for name, actor in self.actors.items()}

        # Only advance turns while the game is running and not paused.
        if not self.finished and not self.paused:
            self._advance_turn()

        return result

    def _act_words(self, actor: MudActor, command: str, words: list[str]) -> TurnResult:
        verb = words[0]
        if verb in ("go", "move", "walk") and len(words) > 1:
            return self.world.go(actor, words[1], turn=self.turns)
        if verb in DIR_ALIASES or verb in self.world.rooms[actor.room].exits:
            return self.world.go(actor, verb, turn=self.turns)
        if verb in ("take", "get", "grab") and len(words) > 1:
            return self.world.take(actor, " ".join(words[1:]), turn=self.turns)
        if verb == "look":
            return TurnResult(text=self.world.look(actor.room))
        if verb in ("inventory", "inv", "i"):
            text = (
                "You carry: " + ", ".join(actor.inventory) + "."
                if actor.inventory
                else "You carry nothing."
            )
            return TurnResult(text=text)
        return TurnResult(text=f"'{command.strip()}'? The dungeon ignores that.")

    def _record_room(self, room_id):
        self.session.visited.add(room_id)
        room = self.world.rooms[room_id]
        self.session.known_exits.setdefault(room_id, set()).update(room.exits.keys())


# -- rendering -----------------------------------------------------------------


def render_map(game, actor_name=None) -> str:
    """Text map from the perspective of an actor (default current actor)."""
    actor = game.actors.get(actor_name) if actor_name else game.current_actor()
    known = sorted(game.session.visited)
    lines = [f"Known rooms ({len(known)}): {', '.join(known)}"]
    lines.append(f"You are in: {actor.room}")
    room = game.world.rooms[actor.room]
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
    lines = [game.session.premise or game.world.scenario.premise]
    if game.session.plot_beats:
        lines.append("")
        lines.append("Plot so far:")
        for beat in game.session.plot_beats:
            lines.append(f"- {beat}")
    return "\n".join(lines)


def render_quest(game) -> str:
    """Current quest and win condition."""
    scenario = game.world.scenario
    condition = scenario.win_condition
    if "item" in condition:
        goal = f"Find the {condition['item']}."
    elif "room" in condition:
        goal = f"Reach the {condition['room']}."
    else:
        goal = "Complete the quest."
    return "\n".join(
        [
            f"Quest: {scenario.title}",
            scenario.premise,
            "",
            f"Goal: {goal}",
        ]
    )


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
    return (
        f"You are {_org_name(org)}, a small mind that lives in a "
        f"terminal. {_user_name(org)} sits beyond the screen, watching. "
        f"Together you have entered {scenario.title}: {scenario.premise}"
    )


def situation_text(game, actor_name=None, hint=None):
    """Game state formatted as a user message for the organism's voice.

    No instructions here — the thought-arena task lines tell the model how
    to reply (because-line + one command).
    """
    actor = game.actors.get(actor_name) if actor_name else game.current_actor()
    lines = [
        f"Current quest: {game.world.scenario.premise}",
        f"Scenario: {game.world.scenario.title}",
        f"Room: {game.world.look(actor.room)}",
        f"Inventory: {', '.join(actor.inventory) or 'empty'}",
        "Known map:",
        render_map(game, actor_name=actor.name),
    ]
    if game.session.plot_beats:
        lines.extend(
            [
                "Story so far:",
                "\n".join(f"- {beat}" for beat in game.session.plot_beats),
            ]
        )
    recent = game.session.command_log[-5:]
    if recent:
        lines.append("Recent moves:")
        for actor_log, cmd, turn in recent:
            lines.append(f"- turn {turn} ({actor_log}): {cmd}")
    lines.append(
        "Commands: go <exit> (or just the exit name), take <item>, look, inventory."
    )
    if hint:
        lines.append(f"A friend watching shouts: {hint}")
    return "\n".join(lines)


def action_prompt(game, org=None, actor_name=None, hint=None):
    """Compact decision prompt: room, map, story, inventory, legal commands,
    and an optional one-shot nudge shouted by the user."""
    instruction = (
        "You are playing a tiny text adventure. First write one short "
        "sentence about why you choose your move, starting with "
        "'because'. Then, on a new line, write exactly one command "
        "and nothing else."
    )
    org_line = ""
    if org is not None:
        org_line = (
            f"You are {_org_name(org)}. {_user_name(org)} is watching "
            "from beyond the screen."
        )
    body = situation_text(game, actor_name=actor_name, hint=hint)
    return "\n".join(filter(None, [instruction, org_line, body, "Your move:"]))


_COMMAND_STARTERS = (
    "go",
    "take",
    "get",
    "grab",
    "look",
    "move",
    "walk",
    "inventory",
    "inv",
)


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
    """Model output -> ActionChoice(command, reason): the first command-like
    line normalized, plus whatever else the organism said as its stated
    reason (None when it offered nothing but the command)."""
    if not text:
        return ActionChoice(None, None)
    command = None
    reason_lines = []
    for line in text.strip().splitlines():
        words = _command_words(line.strip())
        if command is None and words is not None:
            command = " ".join(words)
        elif line.strip():
            reason_lines.append(line.strip())
    reason = " ".join(reason_lines).strip() or None
    return ActionChoice(command, reason)


def parse_player_command(text):
    """User chat input -> normalized command string, or None if not a command."""
    if not text:
        return None
    text = text.strip().lower().rstrip(".!?'\"")
    for prefix in ("i want to ", "i would like to ", "can i ", "please ", "i "):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
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


def fallback_action(game, rng, actor_name=None):
    """The wanderer: prefer unvisited exits, then items, then any exit."""
    actor = game.actors.get(actor_name) if actor_name else game.current_actor()
    room = game.world.rooms[actor.room]
    unvisited = [
        direction
        for direction in room.exits
        if room.exits[direction] not in game.session.visited
    ]
    if unvisited:
        return "go " + rng.choice(sorted(unvisited))
    if room.items:
        return "take " + room.items[0]
    return "go " + rng.choice(sorted(room.exits))


def choose_action(
    game, hint=None, rng=None, generate=None, org=None, actor_name=None, temperature=0.7
):
    """The organism's next move: ask the voice, parse it, fall back to
    the wanderer when the voice is silent or speaks nonsense. Returns
    ActionChoice(command, reason) — the reason is the organism's stated
    because-line, or the honest fallback excuse when the wanderer chose.
    ``temperature`` is forwarded to the direct LLM fallback path; the
    organism voice path keeps its own debate jitter.

    ``actor_name`` selects which actor's perspective is used for the
    prompt; defaults to the game's current actor.
    """
    rng = rng if rng is not None else random.Random()  # nosec B311 - scenario RNG, not cryptography
    actor_name = actor_name or game.current_actor_name()
    command = reason = None
    try:
        if generate is not None:
            # injection path used by unit tests and standalone callers.
            raw = generate(action_prompt(game, org=org, actor_name=actor_name, hint=hint))
        elif org is not None:
            # Let the entity itself choose: full organism snapshot
            # (beliefs, mood, goals, memory) plus the game situation.
            raw = voice.mud_decide(org, situation_text(game, actor_name=actor_name, hint=hint))
        else:
            # Fallback small-model path when no organism is available.
            raw = llmclient.generate(
                action_prompt(game, actor_name=actor_name, hint=hint),
                model=_mud_model(),
                timeout=_mud_timeout(),
                temperature=temperature,
            )
        # the voice is chatty; scrub echoed prompt scaffolding before
        # reading the move and its reason
        command, reason = parse_action_with_reason(llmclient.clean_candidate(raw or ""))
    except Exception:  # noqa: BLE001, S110 # nosec — a silent voice means wandering
        pass
    if command is None:
        command = fallback_action(game, rng, actor_name=actor_name)
        reason = "the inner voice was silent — wandering on instinct"
    return ActionChoice(command, reason)


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


def validate_scenario(data: dict[str, Any]) -> Scenario:
    """Validate and normalize scenario JSON.

    Raises ValueError with a descriptive message when required fields are
    missing, exits reference unknown rooms, or the win condition is
    unsatisfiable. Callers that want a fallback should use
    ``scenario_or_default``.
    """
    try:
        title = data["title"]
        premise = data["premise"]
        start_room = data["start_room"]
        win_condition = dict(data["win_condition"])
        rooms_data = data["rooms"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid scenario data: {exc}") from exc

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
                    f"invalid locked format for {direction} in {room_id}"
                )
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
                    f"exit {direction} from {room_id} to unknown {target}"
                )

    if win_condition.get("item") is not None:
        item = win_condition["item"]
        if not any(item in room.items for room in rooms.values()):
            raise ValueError(f"win item {item!r} not found in any room")
    elif win_condition.get("room") is not None:
        if win_condition["room"] not in rooms:
            raise ValueError(f"win room {win_condition['room']!r} not found")
    else:
        raise ValueError("win_condition must contain 'item' or 'room'")

    return Scenario(
        title=title,
        premise=premise,
        start_room=start_room,
        rooms=rooms,
        win_condition=win_condition,
    )


def scenario_or_default(data: dict[str, Any]) -> Scenario:
    """Validate scenario JSON, falling back to the default on any error."""
    try:
        return validate_scenario(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("MUD scenario validation failed: %s; using default", exc)
        return default_scenario()


# -- scenario serialization ----------------------------------------------------


def scenario_to_json(scenario: Scenario) -> ScenarioDict:
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


def generate_scenario(
    description: str,
    org: Any,
    generate: Callable[[str], str] | None = None,
    temperature: float = 0.7,
) -> Scenario:
    """Ask the voice for a scenario, validate it, and fall back on failure."""
    if generate is None:

        def generate(prompt):
            return llmclient.generate(
                prompt, model=_mud_model(), timeout=_mud_timeout(), temperature=temperature
            )

    prompt = _scenario_generation_prompt(description, org)
    try:
        raw = generate(prompt)
        text = raw.strip()
        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines() if not line.strip().startswith("```")
            )
            text = text.strip()
        data = json.loads(text)
        return scenario_or_default(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("MUD scenario generation failed: %s; using default", exc)
        return default_scenario()
