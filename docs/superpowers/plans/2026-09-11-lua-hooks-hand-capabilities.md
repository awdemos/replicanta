# Lua-native capability hooks (tendon hand) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Lua subsystem the single home for module behavior — one Lua runtime per organism, an open event bus, Lua-owned hand policy/vocabulary — with Python reduced to thin capability bridges.

**Architecture:** A new `LuaHost` owns the single hardened runtime, the service registry, the event bus, and dispatch for both `scripts/*.lua` and `modules/*/init.lua`. `HookEngine`/`ModuleLoader` become facades over it (optional `host=` kwarg; standalone behavior unchanged). `ArmService` gains a complete, table-friendly API and a `set_decide(fn)` policy hook called by the Python timing loop under the host lock. The tendon-hand Lua module installs the policy, sources vocabulary from the service, and declares/emits `hand_goal`/`hand_error`.

**Tech Stack:** Python 3.14, lupa 2.8 (LuaRuntime), pytest, ruff. Spec: `docs/superpowers/specs/2026-09-11-lua-hooks-hand-capabilities-design.md`.

**Baseline:** 938 tests passing, ruff clean, branch `desloppify/code-health-lua-hooks`. Prerequisite bug fixes (table conversion `lua_sandbox.to_py`, `ArmService.moves()/postures()`, single-HookEngine wiring) are already merged on this branch.

---

### Task 1: ArmService — `health()`, cached `state()`, `telemetry()`

**Files:**
- Modify: `src/replicanta/tendon_hand.py` (after `get_state()`, ~line 128; after `_record_telemetry`, ~line 300)
- Test: `tests/test_arm_service.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_arm_service.py`:

```python
"""Service-level tests for the ArmService capability bridge."""

from replicanta.tendon_hand import ArmService


def _stub_request(service, payloads):
    """Capture HTTP traffic: payloads.get(path) -> response dict or Exception."""
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        result = payloads.get(path)
        if isinstance(result, Exception):
            raise result
        return result if result is not None else {}

    service._request = fake
    return calls


def test_health_reports_ok_from_bridge():
    arm = ArmService()
    calls = _stub_request(arm, {"/healthz": {"ok": True}})
    assert arm.health() == {"ok": True, "connected": True}
    assert calls == [("GET", "/healthz", None)]


def test_health_reports_offline_on_error():
    arm = ArmService()
    _stub_request(arm, {"/healthz": ConnectionRefusedError("no bridge")})
    result = arm.health()
    assert result["connected"] is False
    assert "no bridge" in result["error"]


def test_state_uses_cached_sse_snapshot_without_http():
    arm = ArmService()
    calls = _stub_request(arm, {})  # any HTTP call would be recorded
    arm._state = {"goal": "wave", "fingers": {"index": {"joints": {}}}, "emotion": {"mood": "calm"}}
    state = arm.state()
    assert state.goal == "wave"
    assert state.emotion.mood == "calm"
    assert calls == []  # cache hit: no HTTP


def test_state_falls_back_to_http_before_first_sse_frame():
    arm = ArmService()
    calls = _stub_request(arm, {"/arm": {"goal": "fist", "fingers": {}}})
    state = arm.state()
    assert state.goal == "fist"
    assert calls == [("GET", "/arm", None)]


def test_telemetry_returns_recent_samples():
    arm = ArmService()
    arm._telemetry_log = [(1.0, "index_tension", 0.5), (1.0, "goal", "wave")]
    assert arm.telemetry() == [(1.0, "index_tension", 0.5), (1.0, "goal", "wave")]
    arm.telemetry().clear()  # a copy: mutating it must not drain the service
    assert len(arm.telemetry()) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_arm_service.py -q`
Expected: FAIL — `AttributeError: 'ArmService' object has no attribute 'health'` (and `state`/`telemetry` likewise).

- [ ] **Step 3: Write minimal implementation**

In `src/replicanta/tendon_hand.py`, add after `get_state()`:

```python
    def health(self):
        """Bridge liveness probe; never raises (offline -> connected=false)."""
        try:
            raw = self._get_json("/healthz")
            raw.setdefault("connected", True)
            return raw
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "connected": False, "error": str(exc)}

    def state(self):
        """Latest hand state: the SSE-cached snapshot when available, else
        a blocking HTTP fetch (only before the first SSE frame arrives)."""
        if self._state is not None:
            return _DictProxy(self._state)
        return self.get_state()

    def telemetry(self):
        """Recent (time, key, value) samples recorded from SSE frames."""
        with self._lock:
            return list(self._telemetry_log)
```

Note: `/healthz` is served by `robot-hand/bridge/server.py` (`do_GET`). No changes needed there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_arm_service.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/replicanta/tendon_hand.py tests/test_arm_service.py
git commit -m "arm: expose health(), cached state(), telemetry() to Lua"
```

---

### Task 2: ArmService — `set_decide(fn)` Lua policy hook

**Files:**
- Modify: `src/replicanta/tendon_hand.py` (`__init__` ~line 93, after `_decide` ~line 370, and the `_volition_loop` call site ~line 326)
- Test: `tests/test_arm_service.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_arm_service.py`:

```python
def test_set_decide_overrides_volition_choice():
    arm = ArmService()
    arm.set_decide(lambda inputs: "wave" if inputs["stress"] < 0.5 else "fist")
    assert arm._choose("calm", 0.1, 0.1, 0.1, False) == "wave"
    assert arm._choose("calm", 0.9, 0.1, 0.1, False) == "fist"


def test_set_decide_none_restores_default_policy():
    arm = ArmService()
    arm.set_decide(lambda _inputs: "wave")
    arm.set_decide(None)
    # default policy: high stress -> protective fist
    assert arm._choose("calm", 0.9, 0.0, 0.0, False) == "fist"


def test_decide_fn_receiving_dict_returns_none_for_no_move():
    arm = ArmService()
    arm.set_decide(lambda _inputs: None)
    assert arm._choose("calm", 0.1, 0.1, 0.1, False) is None


def test_decide_fn_gets_live_state_snapshot():
    arm = ArmService()
    seen = []
    arm.set_decide(lambda inputs: seen.append(inputs.get("state")) or "point")
    arm._state = {"goal": "reach"}
    arm._choose("calm", 0.1, 0.1, 0.1, False)
    assert seen == [{"goal": "reach"}]


def test_decide_call_uses_configured_lock():
    import threading

    arm = ArmService(lua_lock=threading.Lock())
    with arm._lua_lock:
        arm.set_decide(lambda _inputs: "ok")
    assert arm._choose("calm", 0.1, 0.1, 0.1, False) == "ok"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_arm_service.py -q`
Expected: FAIL — `TypeError: ArmService.__init__() got an unexpected keyword argument 'lua_lock'` / `AttributeError: ... has no attribute 'set_decide'`.

- [ ] **Step 3: Write minimal implementation**

In `ArmService.__init__`, add the parameter and fields:

```python
    def __init__(self, organism=None, bridge_url="http://127.0.0.1:8765", tick_hz=2.0, lua_lock=None):
        ...
        self._lua_lock = lua_lock if lua_lock is not None else threading.Lock()
        self._decide_fn = None  # Lua-installed volition policy (see set_decide)
```

Add the public setter and chooser (place right after `volition()`):

```python
    def set_decide(self, fn):
        """Install (or clear, with None) the volition policy function.

        Called with a dict of inputs (mood, stress, arousal, chaos, insane,
        state); must return a move name string or None (no move this tick).
        Invoked under ``lua_lock`` because the function usually lives in the
        shared Lua runtime, which the host serializes with this lock.
        """
        self._decide_fn = fn

    def _choose(self, mood, stress, arousal, chaos, insane):
        if self._decide_fn is not None:
            inputs = {
                "mood": mood,
                "stress": stress,
                "arousal": arousal,
                "chaos": chaos,
                "insane": insane,
                "state": self._state,
            }
            with self._lua_lock:
                result = self._decide_fn(inputs)
            return result if isinstance(result, str) and result else None
        return self._decide(mood, stress, arousal, chaos, insane)
```

In `_volition_loop`, replace `kind = self._decide(mood, stress, arousal, chaos, insane)` with:

```python
            kind = self._choose(mood, stress, arousal, chaos, insane)
```

Also update the class docstring method list to include `state(), health(), telemetry(), set_decide(fn)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_arm_service.py tests/test_tendon_hand_directives.py -q`
Expected: all pass (new 5 + existing).

- [ ] **Step 5: Commit**

```bash
git add src/replicanta/tendon_hand.py tests/test_arm_service.py
git commit -m "arm: set_decide() policy hook with lock-serialized Lua calls"
```

---

### Task 3: HookService — open event bus

**Files:**
- Modify: `src/replicanta/modules.py` (`HookService`, lines 29-59)
- Test: `tests/test_module_hooks.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_module_hooks.py`:

```python
def test_dynamic_event_subscribe_and_emit():
    hooks = HookService()
    called = []
    hooks.on("hand_goal", lambda text: called.append(text))
    hooks.emit("hand_goal", "wave")
    assert called == ["wave"]


def test_declare_marks_event_first_class_and_known_lists_it():
    hooks = HookService()
    assert "hand_goal" not in hooks.known()
    hooks.declare("hand_goal")
    assert "hand_goal" in hooks.known()
    assert "birth" in hooks.known()  # core events stay known


def test_undeclared_event_still_emits_and_debug_logs(caplog):
    import logging

    hooks = HookService()
    called = []
    hooks.on("whatever", lambda text: called.append(text))
    with caplog.at_level(logging.DEBUG, logger="replicanta.modules"):
        hooks.emit("whatever", "x")
    assert called == ["x"]  # dynamism is not blocked...
    assert any("whatever" in rec.message for rec in caplog.records)  # ...but logged for typos


def test_declare_is_idempotent():
    hooks = HookService()
    hooks.declare("hand_goal")
    hooks.declare("hand_goal")
    assert hooks.known().count("hand_goal") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_module_hooks.py -q`
Expected: FAIL — `on("hand_goal", ...)` logs a warning and drops the handler (dynamic test gets `called == []`); `declare`/`known` raise `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

Replace `HookService` in `src/replicanta/modules.py`:

```python
class HookService:
    """Open event bus used by modules and consumed by HookEngine/LuaHost.

    Core lifecycle events (EVENTS) are always valid; modules may additionally
    declare custom event names (declare) and emit/subscribe any string.
    Undeclared emits work but log at debug level so typos on core events are
    catchable without blocking dynamism.
    """

    EVENTS = (
        "birth",
        "cycle",
        "learned",
        "utterance",
        "fade",
        "mud_turn",
        "mud_win",
        "mud_end",
    )

    def __init__(self):
        self._handlers = {e: [] for e in self.EVENTS}
        self._declared = set(self.EVENTS)

    def declare(self, name):
        """Register a first-class event name (idempotent)."""
        self._declared.add(str(name))

    def known(self):
        """All event names currently known to the bus."""
        return sorted(self._declared)

    def on(self, event, handler):
        self._handlers.setdefault(str(event), []).append(handler)

    def emit(self, event, text=None):
        event = str(event)
        if event not in self._declared:
            logger.debug("undeclared hook event emitted: %s", event)
        for handler in self._handlers.get(event, []):
            try:
                handler(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hook handler for %s failed: %s", event, exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_module_hooks.py -q`
Expected: all pass (4 new + 3 existing).

- [ ] **Step 5: Commit**

```bash
git add src/replicanta/modules.py tests/test_module_hooks.py
git commit -m "hooks: open event bus (declare/on/emit/known, dynamic events)"
```

---

### Task 4: `ctx.events` in the module context

**Files:**
- Modify: `src/replicanta/modules.py` (`ModuleLoader.__init__` ~line 94, `_register_builtin_services` ~line 219, `_build_context` ~line 269)
- Test: `tests/test_modules.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_modules.py` (follow that file's existing ModuleLoader-with-tmp-dir fixture patterns; if it has a helper for writing a module dir, reuse it):

```python
def test_module_context_exposes_events(tmp_path):
    mod = tmp_path / "mods" / "evt"
    mod.mkdir(parents=True)
    (mod / "manifest.toml").write_text('name = "evt"\n')
    (mod / "init.lua").write_text(
        'function init(ctx)\n'
        '  ctx.events:declare("ping")\n'
        '  ctx.events:on("ping", function(text) ctx.log("pong:" .. tostring(text)) end)\n'
        'end\n'
    )
    logs = []
    loader = ModuleLoader(tmp_path / "mods", emit=logs.append, modules_config={"enabled": ["evt"]})
    loader.load_all()
    loader.registry.get("hooks").emit("ping", "hello")
    assert "pong:hello" in logs
    assert "ping" in loader.registry.get("hooks").known()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_modules.py::test_module_context_exposes_events -q`
Expected: FAIL — the init raises inside lupa: `attempt to index field 'events' (a nil value)`; the module lands in `loader.warnings`.

- [ ] **Step 3: Write minimal implementation**

In `ModuleLoader._build_context`, add the events field:

```python
    def _build_context(self, module_name):
        lua = self._runtime()
        return lua.table(
            module_name=module_name,
            log=lambda msg: self.emit(str(msg)),
            services=self.registry,
            events=self.registry.get("hooks"),
        )
```

No other changes needed — the registry's `"hooks"` service is the same `HookService` instance the bus uses, and Lua already calls methods on it via the sandbox attribute handler (same mechanism as `services.get("hooks"):on(...)` in tendon-hand's init.lua).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_modules.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/replicanta/modules.py tests/test_modules.py
git commit -m "modules: expose ctx.events (the open event bus) to Lua modules"
```

---

### Task 5: LuaHost — one runtime for scripts and modules

**Files:**
- Create: `src/replicanta/lua_host.py`
- Modify: `src/replicanta/hooks.py` (optional `host` kwarg, delegate `fire`/`run`/`reload`)
- Modify: `src/replicanta/modules.py` (`ModuleLoader.__init__` kwarg, `_runtime`, `_register_builtin_services` passes the lock)
- Modify: `src/replicanta/organism.py` (`load()` ~line 1135)
- Test: `tests/test_lua_host.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lua_host.py`:

```python
"""LuaHost: one runtime hosts scripts and modules; dispatch reaches both."""

from pathlib import Path

from replicanta.lua_host import LuaHost


def _write_module(mods: Path, name: str, init_lua: str):
    mod = mods / name
    mod.mkdir(parents=True)
    (mod / "manifest.toml").write_text(f'name = "{name}"\n')
    (mod / "init.lua").write_text(init_lua)


def test_host_loads_modules_and_scripts_on_one_runtime(tmp_path):
    mods = tmp_path / "mods"
    _write_module(
        mods,
        "pinger",
        'function init(ctx)\n'
        '  ctx.events:declare("ping")\n'
        '  ctx.events:on("ping", function(text) ctx.log("mod:" .. tostring(text)) end)\n'
        'end\n',
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "s.lua").write_text(
        'function on_ping(ctx) ctx.log("script:" .. tostring(ctx.text)) end\n'
    )
    logs = []
    host = LuaHost(scripts_dir=scripts, modules_dir=mods, emit=logs.append)
    host.load_modules(modules_config={"enabled": ["pinger"]})
    host.reload_scripts()
    host.fire("ping", text="hello")
    assert "mod:hello" in logs
    assert "script:hello" in logs


def test_host_fire_guards_broken_handlers(tmp_path):
    mods = tmp_path / "mods"
    _write_module(
        mods,
        "breaker",
        'function init(ctx)\n'
        '  ctx.events:on("cycle", function(_text) error("boom") end)\n'
        'end\n',
    )
    logs = []
    host = LuaHost(scripts_dir=tmp_path / "scripts", modules_dir=mods, emit=logs.append)
    host.load_modules(modules_config={"enabled": ["breaker"]})
    host.fire("cycle", text="wake")  # must not raise
    assert any("boom" in line for line in logs)


def test_host_run_executes_script_main(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "tool.lua").write_text('function main(ctx) ctx.log("ran:" .. ctx.event) end\n')
    logs = []
    host = LuaHost(scripts_dir=scripts, modules_dir=tmp_path / "mods", emit=logs.append)
    host.reload_scripts()
    assert host.run("tool.lua", org=None) == "lua: ran tool.lua"
    assert any("ran:lua" in line for line in logs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_lua_host.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'replicanta.lua_host'`.

- [ ] **Step 3: Write minimal implementation (LuaHost)**

Create `src/replicanta/lua_host.py` (final minimal version — the loader registers the builtin services; the host only forces its own event bus and lock-armed arm service to stay authoritative):

```python
"""LuaHost: one Lua runtime per organism hosting scripts and modules.

Owns the hardened runtime, the service registry, the open event bus
(HookService), and dispatch. HookEngine and ModuleLoader delegate here when
attached via their ``host`` parameter; standalone construction still works.
"""

import threading
from pathlib import Path

from replicanta import lua_sandbox, telemetry, tendon_hand
from replicanta.modules import (
    CommandService,
    HookService,
    ModuleLoader,
    PersonaService,
    ServiceRegistry,
    VisualService,
    _StoreService,
)


class LuaHost:
    """Single runtime + registry + event bus for one organism."""

    def __init__(self, scripts_dir=None, modules_dir=None, organism=None, emit=None, root=None):
        self.emit = emit if emit is not None else (lambda _msg: None)
        self.lock = threading.RLock()  # reentrant: module handlers may emit events
        self.lua = lua_sandbox.build_runtime()
        self.registry = ServiceRegistry()
        self.hooks = HookService()
        self.organism = organism
        self.root = root
        self.scripts_dir = Path(scripts_dir) if scripts_dir else None
        self.modules_dir = Path(modules_dir) if modules_dir else None
        self.scripts = []
        self.loader = None

    # -- loading -------------------------------------------------------------
    def reload_scripts(self):
        """Re-read the scripts directory (scripts share the host global table)."""
        self.scripts = (
            sorted(self.scripts_dir.glob("*.lua")) if self.scripts_dir and self.scripts_dir.is_dir() else []
        )

    def load_modules(self, modules_config=None, persona_config=None):
        """Load modules through the facade loader on the host runtime.

        The loader registers its builtin services; afterwards the host's
        event bus is re-registered (authoritative) and adopted as the shared
        registry so services.get and ctx.events see the same objects.
        """
        self.loader = ModuleLoader(
            self.modules_dir,
            organism=self.organism,
            modules_config=modules_config or {},
            persona_config=persona_config or {},
            emit=self.emit,
            root=self.root,
            host=self,
        )
        self.loader.load_all()
        self.loader.registry.register("hooks", self.hooks)
        self.registry = self.loader.registry

    # -- dispatch ------------------------------------------------------------
    def fire(self, event, org=None, text=None):
        """Emit to module subscribers, then classic script on_<event> handlers.

        Never raises: every handler failure becomes one emitted error line."""
        with telemetry.get_tracer(__name__).start_as_current_span("hooks.fire") as span:
            span.set_attribute("hook.event", event)
            span.set_attribute("hook.script_count", len(self.scripts))
            with self.lock:
                self.hooks.emit(event, text)
                handlers = []
                prev = self.lua.globals()[f"on_{event}"]
                same = self.lua.eval("function(a, b) return a == b end")
                for script in self.scripts:
                    try:
                        lua_sandbox.sandboxed_execute(self.lua, script.read_text(), name=script.name)
                        hook = self.lua.globals()[f"on_{event}"]
                        if hook is not None and not same(hook, prev):
                            handlers.append((script.name, hook))
                            prev = hook
                    except Exception as exc:  # noqa: BLE001 — user scripts must never kill the organism
                        self.emit(f"{script.name}: {exc}")
                if org is None:
                    ctx = self.lua.table(event=event, text=text)
                else:
                    try:
                        ctx = self._ctx(org, event, text)
                    except Exception as exc:  # noqa: BLE001
                        self.emit(f"ctx: {exc}")
                        return
                for name, hook in handlers:
                    try:
                        hook(ctx)
                    except Exception as exc:  # noqa: BLE001
                        self.emit(f"{name}: {exc}")

    def _ctx(self, org, event, text):
        m = org.metrics()
        mood = org.store.belief_value("self", "mood", "calm")
        return self.lua.table(
            event=event,
            text=text,
            state=org.lifecycle.state,
            cycle=org.store.cycle,
            mood=mood,
            belief_count=m.belief_count,
            rule_count=m.rule_count,
            score=m.score(),
            chaos=org.store.chaos,
            stress=org.store.stress,
            arousal=org.store.arousal,
            rationality=org.store.rationality,
            irrationality=org.store.irrationality,
            insane=org.store.insane,
            organism=org.dir_path.name,
            activity=self.lua.table_from(dict(org.store.activity)),
            log=lambda msg: self.emit(str(msg)),
            set_chaos=lambda x: self._set_chaos(org, x),
            focus=lambda attr: self._focus(org, attr),
        )

    @staticmethod
    def _set_chaos(org, x):
        org.store.chaos = max(0.0, min(1.0, float(x)))

    @staticmethod
    def _focus(org, attr):
        org.window.focus(attr if attr else None)
        org.store.attention = org.window.pairs

    def run(self, name, org):
        """Run one script's main(ctx) on demand (the /lua command)."""
        if Path(name).name != name or not name.endswith(".lua"):
            return f"/lua: bad script name {name!r} (want a plain *.lua file)"
        if self.scripts_dir is None:
            return "/lua: no scripts directory"
        script = self.scripts_dir / name
        if not script.is_file():
            return f"/lua: no {name} in {self.scripts_dir}"
        with self.lock:
            try:
                lua_sandbox.sandboxed_execute(self.lua, script.read_text(), name=name)
                main = self.lua.globals()["main"]
                if main is not None:
                    main(self._ctx(org, "lua", name) if org is not None else self.lua.table(event="lua", text=name))
                return f"lua: ran {name}"
            except Exception as exc:  # noqa: BLE001
                return f"{name}: {exc}"
```

Note: `tendon_hand` is imported here only for the docstring cross-reference of the arm service registration path (the loader registers it); if the linter flags the unused import, drop it — the loader owns that registration.

- [ ] **Step 4: Wire the facades (make tests pass)**

In `src/replicanta/modules.py`:

```python
class ModuleLoader:
    def __init__(self, ..., host=None):
        ...
        self._host = host
```

`_runtime` becomes:

```python
    def _runtime(self):
        if self._host is not None:
            return self._host.lua
        if self._lua is None:
            self._lua = lua_sandbox.build_runtime()
        return self._lua
```

`_register_builtin_services` passes the lock:

```python
        self.registry.register(
            "arm",
            tendon_hand.ArmService(self.organism, lua_lock=(self._host.lock if self._host else None)),
        )
```

In `src/replicanta/hooks.py`:

```python
class HookEngine:
    def __init__(self, scripts_dir, emit=None, hooks_service=None, host=None):
        ...
        self._host = host
```

`fire` delegates:

```python
    def fire(self, event, org, text=None):
        if self._host is not None:
            self._host.fire(event, org=org, text=text)
            return
        ...  # existing standalone path unchanged
```

`run` delegates:

```python
    def run(self, name, org):
        if self._host is not None:
            return self._host.run(name, org)
        ...  # existing standalone path unchanged
```

`reload` delegates:

```python
    def reload(self):
        if self._host is not None:
            self._host.reload_scripts()
            return
        ...  # existing standalone path unchanged
```

In `src/replicanta/organism.py` `load()`, REPLACE the direct `ModuleLoader` construction and hooks-wiring block (currently lines 1135-1149) with host construction — this removes the double module-load:

```python
        from replicanta.lua_host import LuaHost

        self.lua_host = LuaHost(
            scripts_dir=scripts_dir_for(self.dir_path),
            modules_dir=self._modules_dir(),
            organism=self,
            emit=self._emit_log,
            root=self._root_dir(),
        )
        self.lua_host.load_modules(modules_config=cfg.get("modules", {}), persona_config=cfg.get("persona", {}))
        self.lua_host.reload_scripts()
        self.module_loader = self.lua_host.loader
        self.persona_service = self.lua_host.registry.get("persona")
        # Wire the engine created in __init__ in place so anything attached
        # to it before load() survives.
        self.hooks._host = self.lua_host
        self.hooks.hooks_service = self.lua_host.hooks
        if self.hooks.emit is self._default_hook_emit:
            self.hooks.emit = lambda msg: self.store.record_chat("system", msg)
```

(The function-local import mirrors the lazy-import style already used in this file; move it to module scope if preferred.)

- [ ] **Step 5: Run all tests**

Run: `.venv/bin/pytest tests/test_lua_host.py tests/test_hooks.py tests/test_modules.py tests/test_module_hooks.py tests/test_organism.py -q`
Expected: all pass. Then the full suite must stay green: `.venv/bin/pytest tests -q`.

- [ ] **Step 6: Commit**

```bash
git add src/replicanta/lua_host.py src/replicanta/hooks.py src/replicanta/modules.py src/replicanta/organism.py tests/test_lua_host.py
git commit -m "lua: LuaHost — one runtime hosting scripts and modules, open bus wired"
```

---

### Task 6: tendon-hand module — Lua policy, events, service-sourced state

**Files:**
- Modify: `modules/tendon-hand/init.lua`
- Test: `tests/test_tendon_hand_directives.py` (extend the rig: fake `set_decide`, `moves`, capture `events`)

- [ ] **Step 1: Write the failing tests**

In `tests/test_tendon_hand_directives.py`, extend the `_Arm` fake:

```python
    def set_decide(self, fn):
        self.decide = fn

    def moves(self):
        return "middle_finger, thumbs_up, reach, grasp, release, point, wave, fist, ripple, pinch, shaka, rock, spock, open, ok"
```

The module reads `ctx.events` (a ctx field in production, provided by `ModuleLoader._build_context`; on the rig's Python `_Ctx` object it is a plain attribute). Add an `_Events` fake and thread it through `_Ctx`:

```python
class _Events:
    def __init__(self):
        self.handlers = {}
        self.declared = set()
        self.emitted = []

    def declare(self, name):
        self.declared.add(name)

    def on(self, ev, fn):
        self.handlers.setdefault(ev, []).append(fn)

    def emit(self, ev, text=None):
        self.emitted.append((ev, text))
        for fn in self.handlers.get(ev, []):
            fn(text)
```

Update `_Ctx.__init__` to take and store `events` (`self.events = events`), and update the `rig` fixture to construct `events = _Events()` and pass it: `_Ctx(services, logs, events)`; expose `rig.events = events` on the fixture (set after construction, next to `rig.arm`/`rig.logs` assignments).

Then append tests:

```python
def test_module_installs_lua_decide_policy(rig):
    assert hasattr(rig.arm, "decide") and rig.arm.decide is not None
    # calm + low stress -> wave; high stress -> fist (mirrors POLICY in init.lua)
    assert rig.arm.decide({"mood": "calm", "stress": 0.1, "arousal": 0.1, "chaos": 0.1, "insane": False}) == "wave"
    assert rig.arm.decide({"mood": "calm", "stress": 0.9, "arousal": 0.1, "chaos": 0.1, "insane": False}) == "fist"
    assert rig.arm.decide({"mood": "tired", "stress": 0.1, "arousal": 0.1, "chaos": 0.1, "insane": False}) == "release"


def test_module_declares_and_emits_hand_events(rig):
    assert {"hand_goal", "hand_error"} <= rig.events.declared
    rig.fire('hand.move("wave", 3)')
    assert ("hand_goal", "wave") in rig.events.emitted
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tendon_hand_directives.py -q`
Expected: FAIL — `AttributeError`/`KeyError` on missing `events` in ctx, or no `set_decide` on the arm fake (module never installs a policy); emit assertions fail.

- [ ] **Step 3: Write minimal implementation (init.lua changes)**

In `modules/tendon-hand/init.lua`, inside `init(ctx)` after the arm nil-check:

```lua
  -- Volition policy: the organism's mood decides how the hand moves. The
  -- Python loop owns timing only; these rules own the choice.
  local POLICY = {
    { when = function(i) return i.insane or i.stress > 0.78 or i.chaos > 0.85 end, move = "fist" },
    { when = function(i) return i.stress > 0.55 or i.arousal > 0.75 end, move = "grasp" },
    { when = function(i) return i.mood == "curious" or i.mood == "interested" or i.arousal > 0.5 end, move = "reach" },
    { when = function(i) return (i.mood == "calm" or i.mood == "content") and i.stress < 0.25 end, move = "wave" },
    { when = function(i) return i.mood == "tired" or i.mood == "sleepy" end, move = "release" },
  }
  pcall(function()
    arm:set_decide(function(inputs)
      for _, rule in ipairs(POLICY) do
        if rule.when(inputs) then return rule.move end
      end
      return "point"
    end)
  end)
```

Then wire events. After the `local hand = {}` API section's `services:register("hand", hand)` (or right before the hooks registration), add:

```lua
  local events = ctx.events
  if events ~= nil then
    events:declare("hand_goal")
    events:declare("hand_error")
  end
```

In `hand.move`, after the successful dispatch log line, add:

```lua
      if events ~= nil then events:emit("hand_goal", move) end
```

and in the failure branch:

```lua
      if events ~= nil then events:emit("hand_error", move) end
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tendon_hand_directives.py -q`
Expected: all pass (existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add modules/tendon-hand/init.lua tests/test_tendon_hand_directives.py
git commit -m "tendon-hand: Lua-owned volition policy + hand_goal/hand_error events"
```

---

### Task 7: Documentation

**Files:**
- Modify: `AGENTS.md` (Lua hooks rule), `src/replicanta/hooks.py` module docstring, `modules/tendon-hand/init.lua` header comment

- [ ] **Step 1: Update AGENTS.md**

Replace the rule "Lua hooks run sandboxed. New hooks must respect the Lua sandbox in `lua_sandbox.py`." with:

```
5. **Lua owns module behavior; Python provides capabilities.** Modules are pure
   Lua behind the documented ctx API (`ctx.log`, `ctx.services.get`,
   `ctx.events.declare/on/emit/known`). Python services must stay thin
   bridges (table-in/table-out, never raising into Lua). New hooks must
   respect the Lua sandbox in `lua_sandbox.py` — no os/io/require/load.
```

- [ ] **Step 2: Update docstrings**

`src/replicanta/hooks.py` module docstring: add a paragraph noting that a `LuaHost` (`lua_host.py`) owns the single runtime per organism when wired by `Organism.load()`, that the event bus is open (any event name; `declare` makes it first-class), and that module ctx exposes `ctx.events`.

`modules/tendon-hand/init.lua` header: document the policy table (mood→move rules at the top of `init`), the new events, and `arm:set_decide` semantics.

- [ ] **Step 3: Run full verification**

Run: `.venv/bin/pytest tests -q` (all green) and `.venv/bin/ruff check --ignore I001,UP017 . && .venv/bin/ruff format --check .`

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md src/replicanta/hooks.py modules/tendon-hand/init.lua
git commit -m "docs: Lua-first module architecture (ctx API, open bus, hand policy)"
```

---

## Self-review notes (checked against the spec)

- Spec "one runtime per organism" → Task 5. "Open event bus" → Task 3 (+4 ctx, 5 wiring). "Lua owns behavior" → Task 2 (`set_decide`) + Task 6 (policy/vocabulary/events). "Complete arm API" → Task 1 (+batch-1 fixes already landed). "Boundary contract" → batch-1 `lua_sandbox.to_py` (already merged) used by Task 1/2 paths.
- Error handling: Lua handler guards in `LuaHost.fire`; `health()`/`state()` never raise; `set_decide` failures surface in `_choose` via the volition loop's existing try/except (its `log.debug` is unchanged).
- Type consistency: `HookService.declare/on/emit/known` used identically in Tasks 3, 4, 5, 6; `ArmService(lua_lock=...)` ctor kwarg identical in Tasks 2 and 5; `ctx.events` name identical in Tasks 4 and 6; `_DictProxy` reused for `state()`.
- Explicitly deferred (out of spec scope): removing `HookEngine`/`ModuleLoader` public APIs, manifest schemas, Lua-side HTTP.
