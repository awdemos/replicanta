# Lua-native capability hooks for the tendon hand — design

Date: 2026-09-11
Status: approved (user), pending spec review
Branch: `desloppify/code-health-lua-hooks`

## Goal

Digital entities must use the Lua subsystem as the single place where module
*behavior* lives. Following the Neovim model: a small Python core exposes dumb
capabilities behind a stable, documented Lua API; module programs are pure
Lua. All policy, vocabulary, and orchestration for the robot hand
(`../robot-hand`, bridge at 127.0.0.1:8765) moves out of Python and into
`modules/tendon-hand/init.lua`.

## Principles

1. **One Lua runtime per organism.** Today `HookEngine` (scripts/*.lua) and
   `ModuleLoader` (modules/*/init.lua) each build their own hardened sandbox
   and never share state. They merge into a single host (below).
2. **Python = capability providers only.** Service methods are thin bridges:
   table-in/table-out, never raising into Lua. The sandbox stays hardened
   (no os/io/require); HTTP and threads stay in Python because Lua cannot
   hold them here.
3. **Lua owns decisions.** Move vocabulary, aliases, mood→gesture policy,
   volition decisions, and event reactions are Lua tables/functions.
4. **Open event bus.** Modules (and classic scripts) can declare, emit, and
   subscribe to arbitrary event names instead of the closed 8-name tuple.

## Current defects this design depends on (fixed first, same branch)

- `ArmService.emotion()`/`pose()` reject lupa tables (`isinstance(spec, dict)`),
  so every Lua-side call fails; `learned`-hook emotions are silently swallowed.
- `organism.py` builds `HookEngine` twice; the pre-load engine (which code is
  told to attach to) is discarded by `load()`.
- scallopy `ScallopContext` is thread-affine and can be dropped on the wrong
  thread (unraisable `RuntimeError` in the test suite).
- Move vocabulary is duplicated: `GOALS`/`POSTURES` in
  `src/replicanta/tendon_hand.py` vs `MOVES` in `modules/tendon-hand/init.lua`.

## Architecture

### New unit: `LuaHost` (new file `src/replicanta/lua_host.py`)

Owns, per organism:

- the single hardened Lua runtime (from `lua_sandbox.build_runtime()`);
- the service registry (moved out of `ModuleLoader`);
- the open event bus (moved/expanded from `HookService`);
- script discovery (`scripts/*.lua`) and module discovery
  (`modules/*/manifest.toml` + `init.lua`, topological order by `depends`);
- dispatch: `fire(event, text)` runs module subscribers first, then classic
  script `on_<event>(ctx)` handlers, each wrapped so failures log a system
  line and never propagate. Classic script execution semantics are unchanged
  (all scripts share the host's global table; per-event handlers are
  collected per script and called with a fresh ctx).

`HookEngine` and `ModuleLoader` become thin facades over `LuaHost` so existing
callers (`organism.hooks`, TUI module screen, tests) keep working while the
implementation is unified. They are deprecated in comments, not removed, in
this change.

Why one host: halves sandbox startup cost, removes the split where modules
and scripts cannot see each other, and gives one place to document the Lua
API.

### Documented Lua API (the contract)

Every module `init(ctx)` and script hook receives the same shaped context
(fields, plus functions):

- `ctx.module_name` — module dir name (scripts get their file name via event)
- `ctx.log(msg)` — append a system line to the chat log
- `ctx.services.get(name)` — fetch a capability service (table below)
- `ctx.events.declare(name)` — register a first-class event name
- `ctx.events.on(name, fn)` — subscribe; `fn(text)`; any string accepted,
  undeclared emits log at debug level (typo guard without blocking dynamism)
- `ctx.events.emit(name, text)` — broadcast to module subscribers and to
  classic scripts' `on_<name>` handlers
- `ctx.events.known()` — list of declared + core event names

Core events (backwards compatible): `birth`, `cycle`, `learned`, `utterance`,
`fade`, `mud_turn`, `mud_win`, `mud_end`.

### Capability services (Python, thin)

Registered in the host's registry, all table-in/table-out:

- `arm` — `state()` (cached SSE snapshot proxy, no HTTP on hot path),
  `health()`, `moves()`, `postures()`, `goal(k,d)`, `posture(n,d)`,
  `actuator(f,j,side,a,d)`, `pose(spec)`, `emotion(spec)`,
  `summary()`, `volition(bool)`, `set_decide(fn)` (see below).
- `store`, `persona`, `visual`, `commands` — unchanged behavior, boundary
  contract only.
- `organism` — the organism object (existing).

All return plain data (dict-proxies/scalars) or `nil, err`-safe values; a
raised Python exception inside a service call becomes a Lua error caught by
the caller's `pcall` (module code) or the host's per-handler guard (events).

### Lua-driven hand behavior (tendon-hand module)

`modules/tendon-hand/init.lua` becomes the entire brain of the hand:

- **Vocabulary**: built at init from `arm:moves()`/`arm:postures()` (bridge
  is the source of truth; the local hardcoded list is only a fallback when
  the service doesn't provide one). Aliases/reversal verbs stay Lua.
- **Policy**: a `POLICY` table maps `(mood, stress-band, arousal-band)` to
  moves; `arm:set_decide(function(inputs) ... end)` installs it. `inputs` is
  a Lua table `{mood=, stress=, arousal=, chaos=, insane=, state=}`; the
  function returns a move name or `nil` (no move).
- **Volition loop** (Python, `ArmService._volition_loop`): keeps timing only
  (8–16 s by arousal, explicit-move hold). Each tick: if a decide function is
  installed, call it and dispatch the returned goal; else fall back to the
  current Python `_decide` default (kept solely so a bare `ArmService` works
  without the module; never used in practice when the module loads).
- **Reactions** (Lua, via `ctx.events`): `learned` → `arm:emotion{}` (works
  after the table fix); `cycle` wake/sleep → wave/release; `utterance` →
  gesture parsing (existing one-move-per-reply rule unchanged).
- **New events**: the module `declare`s `hand_goal` (emitted after each
  accepted move, text = move name) and `hand_error` (text = reason), so other
  modules can react to the hand without polling.

### Data flow

- **Utterance**: `store.record_chat("org", ...)` → `on_utterance` →
  `LuaHost.fire("utterance", text)` → module subscribers (tendon-hand gesture
  parser) → classic script `on_utterance(ctx)`.
- **Volition**: Python tick → Lua decide fn → `arm:goal()` → HTTP POST →
  bridge; module then `ctx.events.emit("hand_goal", move)`.
- **State reads**: Lua `arm:state()` reads the SSE-cached snapshot (updated by
  the Python listener); falls back to a blocking HTTP GET only before the
  first SSE frame.

### Error handling

- Lua handlers: per-call `pcall` in the host; errors become system log lines.
- Service boundary: converter (`lua_sandbox.to_py`) normalizes tables; Python
  exceptions propagate to Lua as raised errors, caught by module `pcall` or
  host guards. No silent swallowing: caught errors are logged once.
- Bridge offline: `state()`/`health()` return `{connected=false, error=...}`;
  moves return `false` + reason line; volition skips ticks while offline.

### Testing (TDD, pytest)

- `tests/test_lua_host.py` (new): one runtime hosts scripts + modules;
  `fire` reaches both; `events.declare/on/emit` across modules and into
  scripts; facade compatibility (existing HookEngine/ModuleLoader call sites).
- `tests/test_tendon_hand_directives.py`: `set_decide` Lua policy drives the
  goal endpoint; vocabulary sourced from service; `learned` emotion payload
  delivered (regression for the table bug).
- `tests/test_module_hooks.py`: open-bus dynamic events (declare, emit,
  cross-module receive), typo-guard debug behavior.
- Full suite stays green (923 + new), zero unraisable warnings,
  `ruff check --ignore I001,UP017 .` clean.

### Rollout

1. Land prerequisite bug fixes (in flight on this branch).
2. `lua_sandbox.to_py` + ArmService table acceptance + cached `state()`.
3. `LuaHost` + facades; organism wires one host.
4. Open event bus; `ctx.events`.
5. tendon-hand Lua policy/vocabulary/reactions + new events.
6. Docs: Lua API section in module README / `AGENTS.md` if it drifts.

### Out of scope

- No Lua HTTP/socket library, no coroutine-based threading (sandbox stays
  closed). Python keeps loops/IO.
- No manifest schema system (capabilities stay code-registered).
- No removal of `HookEngine`/`ModuleLoader` public APIs (facades only).
