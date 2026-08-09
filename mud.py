"""MUD mode: the organism plays a tiny text adventure in the chat window.

/mud toggles it. The world is deterministic (rooms, exits, items, one
locked gate); the player is the organism's voice — a compact prompt
describes the room and the legal commands, the model replies with one
command, and a forgiving parser executes it. When the voice is offline
or answers nonsense, a random wanderer steps in so the game never
stalls. Moves need little brains: REPLICANTA_MUD_MODEL defaults to a
small fast model, REPLICANTA_MUD_TIMEOUT caps each decision.
"""

import os
import random

MUD_MODEL = os.environ.get("REPLICANTA_MUD_MODEL", "qwen2.5:3b")
MUD_TIMEOUT = int(os.environ.get("REPLICANTA_MUD_TIMEOUT", "60"))

ROOMS = {
    "clearing": {
        "desc": ("A mossy clearing under a flat grey sky. "
                 "A cave yawns to the north."),
        "exits": {"north": "cave mouth"},
        "items": [],
    },
    "cave mouth": {
        "desc": ("The cave's mouth. Cold air breathes out of the dark. "
                 "A torch leans against the rock."),
        "exits": {"south": "clearing", "east": "dark hall"},
        "items": ["torch"],
    },
    "dark hall": {
        "desc": ("A long hall of wet stone. Steps spiral down; "
                 "a rusty gate blocks the north arch."),
        "exits": {"west": "cave mouth", "down": "well room",
                  "north": "treasury"},
        "locked": {"north": ("brass key", "The rusty gate is locked tight.")},
        "items": [],
    },
    "well room": {
        "desc": "A round room around an old well. Something glints on the rim.",
        "exits": {"up": "dark hall"},
        "items": ["brass key"],
    },
    "treasury": {
        "desc": ("A vault glittering with old coins. "
                 "On a pedestal: the amulet."),
        "exits": {"south": "dark hall"},
        "items": ["amulet"],
    },
}

DIR_ALIASES = {"n": "north", "s": "south", "e": "east", "w": "west",
               "u": "up", "d": "down"}


class MudGame:
    """The deterministic half of the game: rooms, inventory, the locked
    gate, the win. Pure — rendering and model calls are the caller's."""

    def __init__(self, start="clearing"):
        # per-game copies: taking an item must not drain the world template
        self.rooms = {name: {**room,
                             "exits": dict(room["exits"]),
                             "items": list(room["items"]),
                             "locked": dict(room.get("locked", {}))}
                      for name, room in ROOMS.items()}
        self.room = start
        self.inventory = []
        self.turns = 0
        self.finished = False
        self.won = False

    def look(self):
        room = self.rooms[self.room]
        bits = [room["desc"]]
        if room["items"]:
            bits.append("You see: " + ", ".join(room["items"]) + ".")
        bits.append("Exits: " + ", ".join(sorted(room["exits"])) + ".")
        return " ".join(bits)

    def act(self, command):
        """One command -> what happened (text). Forgiving parser: exit
        names and single letters work as bare directions, fillers like
        'the' are ignored."""
        self.turns += 1
        words = [w for w in command.strip().lower().split()
                 if w not in ("the", "a", "an", "to")]
        if not words:
            return "Nothing happens."
        verb = words[0]
        if verb in ("go", "move", "walk") and len(words) > 1:
            return self._go(words[1])
        if verb in DIR_ALIASES or verb in self.rooms[self.room]["exits"]:
            return self._go(verb)
        if verb in ("take", "get", "grab") and len(words) > 1:
            return self._take(" ".join(words[1:]))
        if verb == "look":
            return self.look()
        if verb in ("inventory", "inv", "i"):
            return ("You carry: " + ", ".join(self.inventory) + "."
                    if self.inventory else "You carry nothing.")
        return f"'{command.strip()}'? The dungeon ignores that."

    def _go(self, direction):
        direction = DIR_ALIASES.get(direction, direction)
        room = self.rooms[self.room]
        if direction not in room["exits"]:
            return f"You can't go {direction} from here."
        locked = room["locked"].get(direction)
        if locked:
            key, message = locked
            if key not in self.inventory:
                return message
        self.room = room["exits"][direction]
        return self.look()

    def _take(self, item):
        room = self.rooms[self.room]
        for held in list(room["items"]):
            if item in held or held in item:
                room["items"].remove(held)
                self.inventory.append(held)
                if held == "amulet":
                    self.finished = True
                    self.won = True
                    return ("You lift the amulet. The dungeon exhales — "
                            f"you have won, in {self.turns} turns.")
                return f"You take the {held}."
        return f"There is no {item} here."


# -- the player ----------------------------------------------------------------

_FILLER = ("the", "a", "an", "to")


def action_prompt(game, hint=None):
    """Compact decision prompt: room, inventory, legal commands, and an
    optional one-shot nudge shouted by the user."""
    lines = [
        ("You are playing a tiny text adventure. Reply with exactly one "
         "command and nothing else."),
        f"Room: {game.look()}",
        f"Inventory: {', '.join(game.inventory) or 'empty'}",
        ("Commands: go <exit> (or just the exit name), take <item>, look, "
         "inventory. Find the amulet to win."),
    ]
    if hint:
        lines.append(f"A friend watching shouts: {hint}")
    lines.append("Your move:")
    return "\n".join(lines)


def parse_action(text):
    """Model output -> one command string, or None when unusable."""
    if not text:
        return None
    for line in text.strip().lower().splitlines():
        line = line.strip().lstrip(">").strip().strip('"').rstrip(".")
        words = [w for w in line.split() if w not in _FILLER]
        if not words:
            continue
        if (words[0] in ("go", "take", "get", "grab", "look", "move",
                         "walk", "inventory", "inv")
                or words[0] in DIR_ALIASES):
            return " ".join(words)
    return None


def fallback_action(game, rng):
    """The wanderer: take whatever is here, else walk somewhere."""
    room = game.rooms[game.room]
    if room["items"]:
        return "take " + room["items"][0]
    return "go " + rng.choice(sorted(room["exits"]))


def choose_action(game, hint=None, rng=None, generate=None):
    """The organism's next move: ask the voice, parse it, fall back to
    the wanderer when the voice is silent or speaks nonsense."""
    rng = rng if rng is not None else random.Random()
    if generate is None:
        def generate(prompt):
            import narration
            return narration._ollama_generate(
                prompt, model=MUD_MODEL, timeout=MUD_TIMEOUT,
                temperature=0.7)
    raw = None
    try:
        raw = generate(action_prompt(game, hint))
    except Exception:  # noqa: BLE001, S110 — a silent voice means wandering
        pass
    command = parse_action(raw)
    return command if command is not None else fallback_action(game, rng)
