# Agent Guidelines for Replicanta

Replicanta is a local, neurosymbolic AI companion written in Python. It couples a Scallop symbolic reasoner with a local LLM backend, and provides a TUI, web interface, voice/vision, and self-modifying agent hooks.

## Project Layout

```
src/replicanta/    # Core package (organism, memory, hooks, TUI, etc.)
modules/           # Persona modules (software-engineer, creative-writer, socratic-philosopher, visual-state)
scripts/           # Lua hook examples
tests/             # pytest suite
ci/                # Dagger CI module (Go)
docs/              # Assets and superpower plans
replicanta.toml    # Runtime persona config
pyproject.toml     # Project metadata and ruff/pytest config
requirements-ci.txt# Hash-pinned CI dependencies exported from uv.lock
```

## Critical Rules

1. **Dagger CI is the source of truth.** Before committing, run `dagger call ci --source=. --progress=plain` (or the `ci/hooks/pre-commit` hook) to run lint + tests.
2. **Python 3.14 only.** The Scallopy wheel is pinned to CPython 3.14; do not change `requires-python` without rebuilding the wheel.
3. **Scallopy is not on PyPI.** Install the pinned wheel from the v0.1.0 release or place a matching wheel under `wheels/`.
4. **Self-modification is gated.** Auto-apply of code patches is off by default; preserve that default unless the change explicitly addresses agent safety.
5. **Lua owns module behavior; Python provides capabilities.** Modules are pure
   Lua behind the documented ctx API (`ctx.log`, `ctx.services.get`,
   `ctx.events.declare/on/emit/known`). Python services must stay thin
   bridges: table-in/plain-data-out, raising Lua-catchable errors that
   modules wrap in pcall. New hooks must respect the Lua sandbox in
   `lua_sandbox.py` — no os/io/require/load. Modules that need the world
   use the generic capability bridges in `capbridges.py`
   (`ctx.process` managed children incl. the pty recipe, `ctx.http`,
   `ctx.fs` scoped to the organism dir, `ctx.json`); discovery of external
   binaries lives in `externals.py`. Bespoke per-module Python services
   are for genuinely thread-heavy bridges only (tendon-hand).

## Build / Install Commands

```bash
# Create venv and install project + pinned deps
uv venv --python 3.14
uv pip install -e .
uv pip install "https://github.com/awdemos/replicanta/releases/download/v0.1.0/scallopy-0.2.5-cp314-cp314-manylinux_2_39_x86_64.whl#sha256=ddc8d190a55681281f50dffe9f12ef1e04b90a38e1196b786ac2ddb9d7ec51be"
```

## Test Commands

```bash
# Run full test suite
pytest tests -q

# Run a single module
pytest tests/test_hooks.py -q
```

## Lint / Format

```bash
ruff check --ignore I001,UP017 .
ruff format --check .
```

## Running the App

```bash
# TUI
.venv/bin/replicanta

# Web UI
.venv/bin/replicanta --web
```

## CI / Dagger

```bash
# Full CI pipeline (lint + test)
dagger call ci --source=. --progress=plain

# Individual Dagger functions
dagger call test --source=.
dagger call lint --source=.
```

## Gotchas

- `ruff` target is `py312`; newer syntax is accepted but import style is intentionally left un-enforced (`I001` ignored).
- `requirements-ci.txt` is exported from `uv.lock` with hashes; use `uv export --locked --extra dev --no-emit-project --format requirements-txt -o requirements-ci.txt` to refresh.
- Voice/listen/vision extras are optional; the core install does not include them.
- The Scallopy wheel hash is hard-coded in `ci/main.go` and `readme.md`; update both if the wheel changes.

## Module System Notes (ctx API contracts)

- `services.get("organism")` returns a narrow **facade** (`OrganismFacade`
  in modules.py), never the live Organism — its public `Path` attributes
  let sandboxed Lua walk the host filesystem. The facade exposes only
  `name()` and `entity_actuation()` (persisted bool, default True, missing
  on older organisms). `services.get("store")` likewise hides the live
  BeliefStore (observe/remember verbs only).
- **`entity_actuation` gate**: entity-initiated actuation from utterance
  hooks (doom moves, hand.move/posture, brain.run/adapt/bank) checks the
  flag and skips with one log line when off. User paths (/doom, arrow keys,
  /hand, /brain, web buttons) are never gated.
- **No process creation from model text**: the doom-ascii utterance hook
  executes `doom.command(...)` lines for a running game only; `doom.start`
  and `doom.command("start")` in prose are ignored. Games start via /doom,
  arrow keys, or the controller's explicit path.
- `ctx.after(ms, fn)` / `ctx.every(ms, fn)`: daemon timer threads that
  enter the module's runtime with pcall containment. Handlers run **off
  the UI thread** — marshal UI mutations accordingly. `every` returns a
  cancel handle (`h:cancel()` or `h.cancel()`).
- `ctx.kv`: file-backed per-module JSON store at `<organism>/kv/<module>.json`
  (survives reloads). Lua-table facade: `ctx.kv:get(k[, d])`, `:set(k, v)`,
  `:delete(k)`, `:keys()`.
- `ctx.call(line)`: one tolerant parser/router for `svc.method("arg", 2)`
  lines (quote-less single args and `svc.method "arg"` sugar included).
  Returns `result, nil` or `nil, err`; `ctx.call.parse(line)` returns a
  flat `(service, method, *args)` so modules can inspect before executing.
  The doom/fly-brain/tendon-hand utterance parsers share it (legacy local
  parsers remain only as fallbacks for ctx shapes without `ctx.call`).
- `ctx.process.kill(pid)` is **non-blocking** (SIGTERM now; the watcher
  thread reaps and escalates to SIGKILL). Poll `running()`/`wait()` if the
  child must be gone.
- `ServiceRegistry.shutdown()` is idempotent: services' `shutdown()` (or
  `stop()`) then `capbridges.shutdown_all(owner=<this registry>)` — child
  processes are owner-scoped so one organism's module reload never reaps
  another organism's games. Organism.close() calls it.
- The sandbox runtime uses `unpack_returned_tuples=True`: Python callables
  returning tuples deliver multiple Lua values (`local r, err = ctx.call(...)`).
- LuaHost event handlers run under the host lock on the caller's thread —
  a blocking handler stalls ALL delivery. Emits recurse through
  `LuaHost.fire` with a depth guard (8) so utterance→utterance loops drop.
