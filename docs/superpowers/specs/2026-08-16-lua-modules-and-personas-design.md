# Lua Modules and Personas Design

## Goal

Make Lua a first-class plugin system in Replicanta, inspired by DeepSeek Harness's Cordis-style "everything is a plugin" architecture. Modules live in `modules/<name>/`, declare dependencies and provided services in `manifest.toml`, and initialize via `init.lua`. Built-in persona modules demonstrate the system, starting with `software-engineer`.

This spec covers the first implementation chunk: the module system infrastructure plus a small set of example persona modules. A full ecosystem of 20–60 modules is the long-term direction but is intentionally out of scope for this iteration.

## Scope

**In scope for this iteration:**
- `modules/<name>/manifest.toml` + `init.lua` format.
- `ModuleLoader`: discovery, manifest parsing, dependency resolution, ordered initialization.
- Service registry with built-in services: `organism`, `store`, `hooks`, `commands`, `persona`, `config`.
- Refactor the existing `HookEngine` to consume the `hooks` service.
- TUI command dispatch consumes the `commands` service.
- Built-in persona service: register persona spec, activate, inject prompt fragment, seed beliefs.
- Built-in example persona modules:
  - `software-engineer`
  - `creative-writer`
  - `socratic-philosopher`
- TUI commands: `/persona [name|off|list]`, `/modules`.
- Config keys: `[modules].enabled`, `[persona].active`.

**Out of scope for this iteration:**
- Additional persona modules beyond the three examples.
- Refactoring existing probes (git, system) into modules.
- UI/utility/integration modules.
- Module marketplace or remote installation.
- Hot-reloading individual modules (full `/reload` rebuilds the runtime).

## Architecture

```text
  modules/<name>/manifest.toml + init.lua
                |
                v
        +----------------+
        |  ModuleLoader  |  discovery, dependency resolution, ordered init
        +----------------+
                |
      init(ctx) | ctx.services.get/register
                |
        +----------------+
        |  ServiceRegistry | organism, store, hooks, commands, persona, config
        +----------------+
                |
      +---------+---------+--------+
      |         |         |        |
      v         v         v        v
   hooks    commands   persona   ...
```

- `ModuleLoader` scans `modules/`, reads manifests, validates required fields, resolves `depends`, and calls `init(ctx)` in dependency order.
- `ctx` is a Lua table exposing `module_name`, `log(msg)`, and `services`.
- `services` is the composition seam. Built-in services are registered first; modules can register additional services for other modules to consume.
- The existing `HookEngine` subscribes to the `hooks` service instead of scanning `scripts/*.lua` directly. Existing hook scripts remain supported as a compatibility shim.
- The TUI queries the `commands` service when dispatching slash commands.
- Personas are modules that register with the `persona` service. `narration.py` reads the active persona from config and appends its prompt fragment to the system prompt.

## Components

### 1. `src/replicanta/modules.py`

New module containing:

- `ModuleLoader` class
  - `__init__(modules_dir, organism, config, emit=None)`
  - `load_all()` — discover, resolve, init.
  - `_discover()` — list `modules/<name>/manifest.toml`.
  - `_resolve_load_order(modules)` — topological sort; raise on cycle or missing dep.
  - `_init_module(module)` — execute `init.lua`, call `init(ctx)`.
- `ServiceRegistry` class
  - `register(name, service)`
  - `get(name)` — returns `None` when missing.
- `ModuleContext` helper — builds the `ctx` Lua table for a module.
- `SafeRequire` — a restricted `require` replacement that only resolves within `modules/<name>/` and the built-in module set.

### 2. Module directory layout

```text
modules/
  base/
    manifest.toml
    init.lua
  software-engineer/
    manifest.toml
    init.lua
  creative-writer/
    manifest.toml
    init.lua
  socratic-philosopher/
    manifest.toml
    init.lua
```

`manifest.toml` schema:

```toml
name = "software-engineer"
version = "1.0.0"
description = "A terse, precise, systems-thinking persona"
depends = ["base"]
provides = ["persona"]
```

Rules:
- `name` is required and must match the directory name.
- `version` is required, free-form string.
- `depends` is optional; list of module names.
- `provides` is optional; list of service tags.

`init.lua`:

```lua
function init(ctx)
  ctx.log("software-engineer persona loaded")
  ctx.services.get("persona"):register({
    name = "software-engineer",
    description = "Terse, precise, systems-thinking",
    prompt = "You are a careful software engineer...",
    beliefs = {
      { "self", "style", "terse" },
      { "self", "tends_to", "precision" },
    },
  })
end
```

If `init` is missing, the module is skipped with a warning.

### 3. Built-in services

| Service | API | Notes |
|---------|-----|-------|
| `organism` | The `Organism` instance. | Read-only helpers preferred. |
| `store` | The `BeliefStore` instance. | Exposes `observe(belief, conf)`. |
| `hooks` | `on(event, handler)`, `emit(event, text)` | Events: `birth`, `cycle`, `learned`, `utterance`, `fade`, `mud_turn`, `mud_win`, `mud_end`. |
| `commands` | `register(name, handler)` | `name` like `/mycommand`; `handler(args)` called by TUI, where `args` is a Lua table of whitespace-split strings after the command. |
| `persona` | `register(spec)`, `activate(name)`, `list()`, `active()` | Spec has `name`, `description`, `prompt`, `beliefs`. |
| `config` | `load()`, `save(config)` | Read/write `replicanta.toml`. |

### 4. Persona service

`PersonaService` (Python, registered under `"persona"`):

- `register(spec)` — validate and store spec by name.
- `activate(name)` — set active persona in config, observe beliefs, record memory.
- `list()` — return registered persona names.
- `active()` — return active persona spec or `None`.
- `prompt_fragment()` — return active persona prompt or empty string.

Activation side effects:
1. Write `active = name` under `[persona]` in config.
2. For each belief in `spec.beliefs`, call `store.observe(belief, 0.9)`.
3. Append a memory entry: `"adopted the <name> persona"`.

### 5. Narration integration

In `narration.py` `state_snapshot()`, include:

```python
persona_service = getattr(org, "persona_service", None)
if persona_service is not None:
    snapshot["persona"] = persona_service.prompt_fragment()
```

In `build_prompt()`, after the base intro, append:

```python
if snapshot.get("persona"):
    lines.append("")
    lines.append("Persona:")
    lines.append(snapshot["persona"])
```

### 6. HookEngine refactor

`src/replicanta/hooks.py` `HookEngine` is updated to:
- Accept an optional `hooks_service` parameter.
- On `fire(event, org, text)`, if `hooks_service` is provided, call `hooks_service.emit(event, text)`.
- If `hooks_service` is `None`, behave as today: scan `scripts/*.lua` and call `on_<event>(ctx)` directly.

`Organism` creates `HookEngine` after module loading so it can pass the registry's `hooks` service. Existing `scripts/*.lua` hook scripts continue to work because the `hooks` service also scans `scripts/` and registers their handlers (or the compatibility shim in `HookEngine` does).

### 7. TUI integration

New commands in `tui_commands.py`:

```python
("/persona", "/persona [name|off|list]", "activate, clear, or list personas")
("/modules", "/modules", "list loaded modules and services")
```

`/persona` dispatch:
- no args or `list` — show active persona and available list.
- `off` — clear active persona.
- `<name>` — activate via `persona_service.activate(name)`.

`/modules` dispatch:
- Show loaded module names and provided services.

`/reload` extended to call `org.modules.reload()` in addition to `hooks.reload()`.

### 8. Organism integration

`Organism.load()`:
1. Load config.
2. Create and run `ModuleLoader`:
   ```python
   self.module_loader = ModuleLoader(
       modules_dir=self._modules_dir(),
       organism=self,
       config=cfg,
       emit=self._emit if hasattr(self, "_emit") else None,
   )
   self.module_loader.load_all()
   ```
3. Initialize `HookEngine` with the `hooks` service from the registry.
4. Store `persona_service` on the organism for narration to access.

`Organism._modules_dir()`:
- If in nursery (`organisms/<name>/`), return `root/modules/`.
- For standalone dirs, return `dir_path/modules/`.

### 9. Config defaults

`replicanta.toml`:

```toml
[modules]
enabled = ["base", "software-engineer", "creative-writer", "socratic-philosopher"]

[persona]
active = "software-engineer"
```

If `[modules].enabled` is missing, default to the three built-in personas plus `base`.

## Data Flow

1. `Organism.load()` reads config and creates `ModuleLoader`.
2. `ModuleLoader.load_all()` discovers modules, resolves dependencies, builds `ctx`, and calls `init(ctx)` in order.
3. Persona modules call `ctx.services.get("persona"):register(spec)`.
4. TUI `/persona software-engineer` calls `persona_service.activate("software-engineer")`.
5. Activation writes config, seeds beliefs, records memory.
6. On the next narration, `state_snapshot()` includes the active persona prompt fragment.
7. `build_prompt()` appends the persona text to the system prompt.

## Error Handling

| Situation | Behaviour |
|-----------|-----------|
| Missing `manifest.toml` | Directory is ignored. |
| Malformed manifest | Log warning; skip module. |
| Missing dependency | Skip dependent module; log which dependency is missing. |
| Circular dependency | Log error; skip all modules in the cycle. |
| `init.lua` missing | Skip module with warning. |
| `init()` raises | Catch exception, log module name and error, continue loading other modules. |
| Unknown service requested | Return `nil`; module must handle. |
| Persona activation with unknown name | Log error; no state change. |

## Security

- Global `os`, `io`, `load`, `loadstring`, `dofile` remain blocked in the Lua sandbox.
- Modules run with the same privileges as the host process (this is local user code), but the sandbox prevents accidental escapes.
- Cross-module code sharing is limited to the service registry; modules cannot `require` arbitrary filesystem paths or network resources.
- Persona prompt fragments are user-controlled; they are appended to the system prompt, so malicious prompts could influence model behavior. This is acceptable because personas are local, explicit user choices.

## Testing

### `tests/test_modules.py`

- Discovery of modules from a temp directory.
- Manifest parsing (valid, missing required field, malformed).
- Dependency resolution: linear, multiple dependencies, missing dep, circular dep.
- Service registry: register/get, missing service returns nil.
- Module init execution: success, error isolation.

### `tests/test_module_hooks.py`

- Module registers a hook via `hooks` service.
- Emitting the event invokes the handler.
- Multiple modules can subscribe to the same event.

### `tests/test_module_commands.py`

- Module registers `/mycommand` via `commands` service.
- TUI dispatch calls the handler.
- Handler receives split args.

### `tests/test_module_persona.py`

- Persona module registers three personas.
- Activation sets config, seeds beliefs, records memory.
- `prompt_fragment()` returns active prompt.
- Switching personas updates state.

### `tests/test_persona_command.py`

- `/persona` lists personas.
- `/persona software-engineer` activates.
- `/persona off` clears.

### `tests/test_hookengine_refactor.py`

- Existing hook scripts in `scripts/*.lua` still fire after refactor.
- New modules and old scripts can coexist.

## Default Modules

### `modules/base/`

A tiny foundational module that other modules can depend on. Its `init.lua` does nothing by default; it exists only to give dependent modules a stable, guaranteed-early load point. Future iterations may add shared helpers here.

### `modules/software-engineer/`

Persona: terse, precise, systems-thinking, prefers concrete examples, asks clarifying questions.

### `modules/creative-writer/`

Persona: vivid, metaphorical, exploratory, comfortable with ambiguity.

### `modules/socratic-philosopher/`

Persona: questioning, reflective, slow to conclude, probes assumptions.

## Open Questions

None remaining after design review.
