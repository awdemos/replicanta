# MUD Redesign Plan: Choose-Your-Own Dungeon Crawl

> **For agentic workers:** Implement in the phases below. Run `tests/test_mud.py` after every phase and the full suite before finishing.

**Goal:** Transform the deterministic `/mud` mini-game into an interactive, user-shaped text adventure where the user's words are direct commands or plot twists, the organism and user are characters in the story, map/story state is tracked and persisted, and the user can propose custom scenarios.

**Scope:** `mud.py` engine refactor, TUI command handling, organism memory/hook integration, scenario generation via LLM, persistence of map/story state, and new tests.

**Tech Stack:** Python 3.14, Textual, Ollama, existing `narration`/`arena` modules, Scallop organism store.

---

## File map

- `mud.py` — scenario data model, `MudGame` refactor, player-command parser, move prompts, scenario-generation prompt.
- `tui.py` — `/mud` subcommands, user-input routing in MUD mode, map/story rendering, persistence calls.
- `tui_commands.py` — update help text and command palette entries.
- `organism.py` — load/save MUD state artifact, emit `remember("mud", ...)` events.
- `hooks.py` — add `mud_turn`, `mud_win`, `mud_end` hook events.
- `tests/test_mud.py` — extended regression tests for new engine behavior.
- `tests/test_mud_scenarios.py` — new tests for scenario generation and persistence.

---

## Design decisions

1. **Command hierarchy while MUD is active**
   - Lines starting with `/mud ` are MUD meta-commands (`map`, `story`, `quest`, `scenario`, `reset`, `pause`, `resume`, `step`).
   - Bare `/mud` still toggles MUD on/off.
   - Other chat lines are parsed as MUD game commands (`go <dir>`, `<dir>`, `take <item>`, `look`, `inventory`). If parseable, they execute immediately and override the next scheduled organism turn. If not parseable, they become a narrative hint for the organism's next move (current behavior).

2. **State ownership**
   - The live game object (`MudGame`) owns transient state.
   - A serializable `MudSession` object tracks visited rooms, discovered exits, plot beats, inventory log, command history, and outcome.
   - The session is persisted to `<organism-dir>/artifacts/mud_state.json` and reloaded when `/mud` starts.

3. **Scenario generation**
   - User provides a setting/quest with `/mud scenario <description>`.
   - The LLM is asked for compact JSON (5–8 rooms) via `narration._ollama_generate` using the existing `REPLICANTA_MUD_MODEL`.
   - A small deterministic validator normalizes and falls back to the built-in scenario if JSON is malformed.

4. **Story/plot**
   - Each scenario has a `premise` that names the organism and the user.
   - Rooms and items may carry optional `plot_trigger` text.
   - Plot beats are recorded in `MudSession` and displayed with `/mud story`.

---

## Phase 1: Refactor `mud.py` to be scenario-driven

**Files:** `mud.py`, `tests/test_mud.py`

### Step 1: Add scenario/room data classes

```python
from dataclasses import dataclass, field

@dataclass
class Room:
    desc: str
    exits: dict[str, str] = field(default_factory=dict)
    items: list[str] = field(default_factory=list)
    locked: dict[str, tuple[str, str]] = field(default_factory=dict)
    plot_trigger: str | None = None
    is_goal: bool = False

@dataclass
class Scenario:
    title: str
    premise: str
    start_room: str
    rooms: dict[str, Room]
    win_condition: dict  # e.g. {"item": "amulet"} or {"room": "treasury"}
```

### Step 2: Convert the built-in world into `DEFAULT_SCENARIO`

Replace the top-level `ROOMS` dict with a function `default_scenario()` returning a `Scenario`. Keep room IDs, exits, items, and locked gate identical so the existing walkthrough still works.

### Step 3: Refactor `MudGame`

```python
class MudGame:
    def __init__(self, scenario=None, session=None):
        self.scenario = scenario or default_scenario()
        self.session = session or MudSession(self.scenario.title)
        self.rooms = deep-copy rooms from scenario
        self.room = self.scenario.start_room
        self.inventory = []
        self.turns = 0
        self.finished = False
        self.won = False
```

Add an `act_event(command)` method that returns a small dataclass:

```python
@dataclass
class TurnResult:
    text: str
    moved: bool = False
    took: str | None = None
    plot: str | None = None
    finished: bool = False
    won: bool = False
```

Keep `act(command)` as a thin wrapper returning `result.text` for backward compatibility.

### Step 4: Track plot triggers

When entering a room for the first time, if the room has a `plot_trigger`, include it in `TurnResult.plot` and record it in `self.session.plot_beats`.

### Step 5: Update tests

- Keep all existing `test_mud.py` assertions passing.
- Add `test_default_scenario_walkthrough` that verifies the classic 8-turn win path still works through the new `Scenario` path.

### Acceptance criteria

- `pytest tests/test_mud.py -q` passes.
- `MudGame()` without arguments still plays the deterministic dungeon.
- `act()` still returns a string.

---

## Phase 2: Map and story tracking

**Files:** `mud.py`, `tui.py`, `tests/test_mud.py`

### Step 1: Add `MudSession`

```python
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

    def to_json(self) -> dict
    @classmethod
    def from_json(cls, data: dict) -> "MudSession"
```

### Step 2: Record discovery on every turn

In `MudGame.act_event`:
- Mark current room visited.
- Record all exits of the current room in `known_exits`.
- Log commands with actor `"organism"` or `"user"`.
- Log inventory changes.

### Step 3: Render map and story

Add pure functions to `mud.py`:

```python
def render_map(game) -> str
def render_story(game) -> str
def render_quest(game) -> str
```

`render_map` produces a compact text list, e.g.:

```
Known rooms (3): clearing, cave mouth, dark hall
You are in: dark hall
Exits seen from here: west, down, north (locked)
```

### Step 4: TUI `/mud map` and `/mud story`

In `tui.py` `/mud` dispatch:
- `/mud map` → log `mud.render_map(game)`.
- `/mud story` → log `mud.render_story(game)`.
- `/mud quest` → log `mud.render_quest(game)`.

### Acceptance criteria

- After moving, `/mud map` shows only rooms the organism has entered.
- `/mud story` shows the scenario premise and any plot triggers seen.
- Session state is internally consistent after a full walkthrough.

---

## Phase 3: User commands and interactivity

**Files:** `tui.py`, `mud.py`, `tui_commands.py`, `tests/test_mud.py`

### Step 1: Parse player commands

Add `parse_player_command(text)` to `mud.py`. It accepts the same vocabulary as `parse_action` plus optional natural prefixes (`"go north"`, `"take the torch"`). Returns a normalized command string or `None`.

### Step 2: Reroute `handle_chat` in MUD mode

Change `handle_chat`:

```python
def handle_chat(self, text):
    self._log_chat("user", text)
    if self._mud_game is not None:
        command = mud.parse_player_command(text)
        if command is not None:
            self._mud_player_command = command
            self._mud_apply(self._mud_game, command, actor="user")
            return
        self._mud_hint = text
    # ... normal organism hearing
```

Add `self._mud_player_command` to store a command that should be consumed by the organism's next turn instead of generating a new one.

### Step 3: Pause/resume/step controls

Add state `self._mud_paused = False`.

- `/mud pause` — stop auto-turns; status bar shows `🗡 mud (paused)`.
- `/mud resume` — resume auto-turns.
- `/mud step` — run exactly one organism turn while paused.
- When paused, only user commands move the game.

### Step 4: Update command registry

In `tui_commands.py`, expand the `/mud` entry:

```python
("/mud", "/mud [map|story|quest|scenario|reset|pause|resume|step]",
 "toggle or control the dungeon crawl")
```

### Acceptance criteria

- Typing `go north` while MUD is active moves the organism immediately.
- Typing `take torch` in the cave mouth picks up the torch.
- Typing `look at the well` (unparseable) becomes a hint for the next organism turn.
- `/mud pause` stops auto-turns; `/mud step` advances one turn.
- `pytest tests/test_mud.py tests/test_tui_commands.py -q` passes.

---

## Phase 4: Plot that introduces the user and organism

**Files:** `mud.py`, `narration.py`, `tui.py`

### Step 1: Build a premise template

Add `build_premise(org)` in `mud.py` or `narration.py` that produces text like:

> "You are `<organism-name>`, a small mind born in a Scallop engine. `<user-name>` sits beyond the screen, watching. Together you have entered `<scenario.title>`: `<scenario.premise>`"

Use `self._org_name()` and a user name from beliefs (`user` object, `name` attribute) or fall back to `"the user"`.

### Step 2: Emit premise on MUD start

In `_toggle_mud`, after creating the game, log the premise as the first story entry.

### Step 3: Add user/organism mentions to move prompt

Update `action_prompt` to include:

```text
You are <organism-name>. <user-name> is watching from beyond the screen.
Current quest: <scenario premise>
```

### Acceptance criteria

- Starting `/mud` logs a premise that names the organism and user.
- The organism's move prompt contains the premise.
- `/mud story` includes the premise at the top.

---

## Phase 5: User-derived scenarios

**Files:** `mud.py`, `tui.py`, `tests/test_mud_scenarios.py`

### Step 1: Scenario-generation prompt

Add `generate_scenario(description, org, generate=None)` in `mud.py`. Prompt asks for JSON:

```text
You are a MUD designer. Create a compact text-adventure scenario (5–8 rooms) as JSON:
{
  "title": "...",
  "premise": "... (mention the organism and the user)",
  "start_room": "room_id",
  "win_condition": {"item": "..."},
  "rooms": {
    "room_id": {
      "desc": "...",
      "exits": {"north": "other_room_id"},
      "items": ["..."],
      "locked": {"east": ["key_item_id", "locked message"]},
      "plot_trigger": "optional text on first entry"
    }
  }
}
Setting: <description>
Keep it winnable in 6–15 turns. Reply with only the JSON.
```

### Step 2: JSON validation and fallback

Add `validate_scenario(data) -> Scenario`. If required fields are missing or exits reference unknown rooms, fall back to `default_scenario()` and log a warning.

### Step 3: `/mud scenario <description>`

In `tui.py`, when `/mud scenario ...` is received:
- If MUD is running, stop the current game.
- Generate or build the scenario.
- Save the scenario JSON to `<organism-dir>/artifacts/mud/scenarios/<slug>.json`.
- Start a new game with the scenario.

### Step 4: Cache and reuse

Store generated scenarios so the same prompt (or slug) can be restarted with `/mud reset`.

### Acceptance criteria

- `/mud scenario a sunken cathedral where the bell is a memory chip` starts a new non-default adventure.
- Malformed LLM output falls back to the default scenario with a log warning.
- Generated scenarios are saved to disk.
- `pytest tests/test_mud_scenarios.py -q` passes.

---

## Phase 6: Persistence and organism memory

**Files:** `organism.py`, `tui.py`, `hooks.py`

### Step 1: Persist `MudSession`

In `tui.py`, after every turn and on `/mud` toggle-off, call:

```python
self.org.store.save_mud_session(self._mud_game.session)
```

Add `BeliefStore.save_mud_session(session)` / `load_mud_session()` that read/write `<dir>/artifacts/mud_state.json`.

### Step 2: Resume on start

When `/mud` is toggled on, if `artifacts/mud_state.json` exists and the user has not requested a new scenario, offer to resume or start fresh. For simplicity: resume automatically if the saved scenario exists, else start fresh.

### Step 3: Remember MUD events

In `_toggle_mud` (start/end) and `_mud_apply` (win), call:

```python
self.org.store.remember("mud", f"started {scenario.title}")
self.org.store.remember("mud", f"won {scenario.title} in {game.turns} turns")
```

### Step 4: Add Lua hook events

In `hooks.py`, extend `EVENTS` with `"mud_turn"`, `"mud_win"`, `"mud_end"`. Fire them from the TUI at the corresponding moments with `text` set to a short event summary.

### Acceptance criteria

- Toggling `/mud` off and on restores the previous room, inventory, visited map, and plot beats.
- Winning a MUD creates an episodic memory entry with kind `"mud"`.
- A Lua `on_mud_win(ctx)` hook runs when the organism wins.

---

## Phase 7: Move-selection prompt improvements

**Files:** `mud.py`

### Step 1: Richer move prompt

`action_prompt(game, org, hint=None)` should include:

- Scenario title and premise.
- Current room description.
- Known map (visited rooms + seen exits).
- Inventory.
- Last 5 commands and outcomes.
- Current plot beats.
- Legal commands.
- Optional user hint/command.

### Step 2: Structured fallback

Keep `fallback_action` but make it slightly smarter: prefer unvisited exits, then items, then wandering.

### Acceptance criteria

- The move prompt text includes map and story context.
- Fallback still produces a legal command.
- `pytest tests/test_mud.py -q` passes.

---

## Phase 8: Integration, help, and verification

**Files:** `tui_commands.py`, `tests/test_mud.py`, `tests/test_mud_scenarios.py`

### Step 1: Update help text

Add the new `/mud` subcommands to `help_text()`.

### Step 2: Full test run

```bash
.venv/bin/python -m pytest tests/test_mud.py tests/test_mud_scenarios.py tests/test_tui_commands.py -q
```

Expected: PASS.

### Step 3: Full suite + lint

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check --ignore I001,UP017 .
```

Expected: all tests pass, lint clean.

### Step 4: Manual smoke test

Run the TUI, type:

1. `/mud` — premise appears.
2. `go north` — user moves the organism.
3. `/mud map` — map shows visited rooms.
4. `/mud story` — recap appears.
5. `/mud scenario a haunted space station` — new scenario starts.
6. `/mud pause` — auto-turns stop.
7. `/mud step` — one organism turn runs.
8. `/mud` — game stops and state persists.

### Acceptance criteria

- All automated tests pass.
- Lint is clean.
- Manual smoke test completes without exceptions.
