# Git-Creature Sensing Design

## Goal

Let organisms in Replicanta sense the git state of the worktree they are running in, similar to how [parallel-harness-pets](https://github.com/TevvvB/parallel-harness-pets) gives every worktree a creature whose mood reflects repo hygiene. The organism should treat git hygiene as another sensory input stream, alongside CPU, memory, and battery.

## Scope

- Sense three git signals: dirty worktree, unpushed commits, commits behind trunk.
- Reactions: symbolic beliefs, stress/distress, mood shifts via the existing stress pipeline, memory episodes, and unprompted mentions through the existing narration/diary cycle.
- Explicitly enabled via TUI commands and a persistent config file.
- Sensible defaults with user-overridable thresholds and weights.

Out of scope for this iteration:
- Species/rarity/status-line rendering like parallel-harness-pets.
- Test/lint failure signals or migration heads.
- Multiple organisms per worktree.

## Architecture

The feature follows the existing `SystemProbe` pattern:

- `SystemProbe` reads `/proc`/`/sys` and returns a snapshot, beliefs, and distress.
- `GitProbe` reads `git` and returns a snapshot, beliefs, and distress.
- The `Organism` optionally holds a `GitProbe`; when enabled, `sense()` folds git beliefs into the store and applies git distress.

```text
  git status / rev-list / rev-parse
            |
            v
      +-------------+
      |  GitProbe   |
      +-------------+
            |
   snapshot | beliefs | distress | summary
            |
            v
      +-------------+
      |  Organism   | <-- config from replicanta.toml
      +-------------+
            |
            v
   belief store, stress meter, memory, mood, narration
```

## Components

### 1. `src/replicanta/gitstate.py`

A new module containing `GitProbe`.

#### `GitProbe.__init__(worktree, config=None, spawn=None)`

- `worktree`: `Path` to the directory to inspect.
- `config`: dict of thresholds/weights; missing keys use defaults.
- `spawn`: injectable subprocess runner for tests; defaults to a private helper that calls `git`.
- Keeps `_prev_adverse` for edge-triggered distress, just like `SystemProbe._adverse_seen`.

#### `GitProbe.snapshot()`

Returns a dict:

```python
{
    "is_repo": True,
    "branch": "main",
    "upstream": "origin/main",  # None if no upstream
    "dirty_count": 3,
    "unpushed_count": 2,        # None if no upstream
    "behind_count": 0,          # None if no upstream
}
```

If the directory is not inside a git repo, returns `{"is_repo": False}`.

Git commands used:

| Field | Command |
|-------|---------|
| `is_repo` / `branch` | `git rev-parse --is-inside-work-tree --abbrev-ref HEAD` |
| `upstream` | `git rev-parse --abbrev-ref HEAD@{upstream}` |
| `dirty_count` | `git status --porcelain` (count non-empty lines) |
| `unpushed_count` | `git rev-list --count HEAD@{upstream}..HEAD` |
| `behind_count` | `git rev-list --count HEAD..HEAD@{upstream}` |

#### `GitProbe.beliefs(snap)`

Quantizes counts into symbolic values the Scallop reasoner accepts (only `a-z_`):

| Signal | Levels |
|--------|--------|
| `dirty` | `none`, `few`, `many` |
| `unpushed` | `none`, `few`, `many` |
| `behind` | `none`, `few`, `many` |

Returns beliefs such as `(git, dirty, few)` with confidence `0.9`.

When there is no upstream, `unpushed` and `behind` are `none`.

#### `GitProbe.distress(snap)`

Edge-triggered stress amount. A condition contributes its weight only when it newly appears; once seen, it does not re-stack on every tick. If the condition clears and later returns, it counts again.

Default weights:

| Condition | Weight |
|-----------|--------|
| `dirty` (1+ file) | 0.05 |
| `dirty_many` (15+ files) | 0.08 |
| `unpushed` (1+ commit) | 0.05 |
| `unpushed_many` (5+ commits) | 0.08 |
| `behind` (1+ commit) | 0.05 |
| `behind_many` (20+ commits) | 0.10 |

Total is capped at `0.25`.

#### `GitProbe.summary(snap)`

Returns a short human string, e.g. `main · 3△ · 2↑`.

### 2. `src/replicanta/config.py`

Loads and saves `replicanta.toml` from the project root using Python 3.14's stdlib `tomllib`.

Functions:

- `load_config(root)` → dict (defaults when file missing/malformed).
- `save_config(root, config)` → write the dict back to `replicanta.toml`, preserving any unrelated sections.

Example:

```toml
[git]
enabled = false
dirty_many_at = 15
unpushed_many_at = 5
behind_many_at = 20
dirty_weight = 0.05
dirty_many_weight = 0.08
unpushed_weight = 0.05
unpushed_many_weight = 0.08
behind_weight = 0.05
behind_many_weight = 0.10
```

Behaviour:

- Missing file → use defaults, no warning.
- Malformed file → warn once, use defaults.
- Unknown keys → ignored.

### 3. `Organism` integration

Changes in `src/replicanta/organism.py`:

- `__init__` accepts an optional `git_probe` argument.
- `load()` reads config and attaches a `GitProbe` when `git.enabled` is true and the worktree is inside a repo.
- `sense()` calls the git probe when present, observes git beliefs, applies git distress, and records a memory if a new adverse condition appears.
- New public methods:
  - `git_enable()` — attach a `GitProbe` and persist `enabled = true` in config. Works even if the worktree is not currently a repo; in that case the probe reports `is_repo: false` and no distress is generated.
  - `git_disable()` — detach and persist `enabled = false`.
  - `git_status()` — return the current `summary()` string, or a "not a repo / disabled" message.

The organism's existing mood computation already reacts to stress, so git distress indirectly shifts mood. Memory entries give the narration pipeline context to mention repo hygiene during the next diary/reflect cycle.

### 4. TUI commands

Add to `src/replicanta/tui_commands.py`:

| Command | Behaviour |
|---------|-----------|
| `/git on` | Enable git sensing for the current organism. |
| `/git off` | Disable git sensing. |
| `/git status` | Print the current git summary in chat. |

Wire them into `src/replicanta/tui.py` `handle_command()`.

## Data Flow

Per tick when enabled:

1. `Organism.tick()` reaches `sense()` every `SENSE_INTERVAL`.
2. `GitProbe.snapshot()` runs the five `git` commands.
3. `GitProbe.beliefs(snap)` quantizes counts into `few` / `many` / `none`.
4. The organism calls `store.observe(belief, conf)` for each git belief.
5. `GitProbe.distress(snap)` computes the edge-triggered stress bump.
6. `StressMeter.bump(distress)` raises stress.
7. If a condition newly appeared, `store.remember("git", text)` records it.
8. Existing machinery converts stress into mood and memory into narration.

## Error Handling

| Situation | Behaviour |
|-----------|-----------|
| Not a git repo | `is_repo: false`; no beliefs, no distress; `/git status` reports "not a git repository". |
| Git binary missing or command fails | Treat as `is_repo: false` for that tick; emit one warning per session. |
| No upstream branch | `unpushed_count` and `behind_count` are `None`; beliefs report `none`; no distress. |
| Config file missing | Use defaults, silently. |
| Config file malformed | Log a warning once, use defaults. |
| Branch names with non-`a-z_` chars | Not stored as beliefs; only appear in `summary()` and memory text. |

## Testing

### `tests/test_gitstate.py`

Unit-test `GitProbe` with an injectable `spawn` callback returning fake `git` output:

- Clean repo, dirty repo, many uncommitted files.
- Unpushed commits, many unpushed commits.
- Behind trunk, many behind.
- No upstream branch.
- Not a git repo.
- Edge-triggered distress: first dirty tick bumps stress; second does not; clearing and re-dirtying counts again.
- Belief quantization respects thresholds.

### `tests/test_config.py`

- Load valid `replicanta.toml` and assert custom thresholds override defaults.
- Missing file returns defaults.
- Malformed file returns defaults and warns.

### `tests/test_organism.py` extension

- Create an `Organism` with a fake `GitProbe`.
- Enable git sensing; run ticks; assert git beliefs appear, distress applies, and memory is recorded.
- Assert `git_disable()` detaches the probe.

### `tests/test_tui_commands.py` extension

- Verify `/git on`, `/git off`, `/git status` are registered in `COMMANDS`.

## Default Configuration

```toml
[git]
enabled = false
dirty_many_at = 15
unpushed_many_at = 5
behind_many_at = 20
dirty_weight = 0.05
dirty_many_weight = 0.08
unpushed_weight = 0.05
unpushed_many_weight = 0.08
behind_weight = 0.05
behind_many_weight = 0.10
```

## Open Questions

None remaining after design review.
