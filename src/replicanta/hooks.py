"""Hooks: a user-scriptable interface for the organism. Lua scripts in
`scripts/*.lua` (at the nursery root, or beside the organism for
standalone dirs) define event functions — on_birth, on_cycle, on_learned,
on_utterance, on_fade — and the engine calls them at the corresponding
moments. MUD sessions fire on_mud_turn, on_mud_win and on_mud_end (with
ctx.text set to a short event summary). Each receives a ctx table:

    ctx.event, ctx.text        -- what happened (+ the words, when any)
    ctx.state, ctx.cycle       -- wake/sleep/dead, lifecycle cycle
    ctx.mood                   -- current mood belief
    ctx.belief_count, ctx.rule_count, ctx.score
    ctx.chaos, ctx.stress
    ctx.organism               -- the organism's nursery dir name
    ctx.activity               -- neurosymbolic activity counters (table)

and safe actuators:

    ctx.log(msg)               -- append a line to the chat log
    ctx.set_chaos(x)           -- retune randomness (clamped to 0..1)
    ctx.focus(attr)            -- steer attention (nil to clear)

Scripts are sandboxed (no os/io/require/load), every hook call is
protected (a broken script logs an error line, never kills the
organism), and a lock makes the single Lua runtime safe against the
TUI's worker threads. `/reload` re-reads the scripts directory.
`/lua name.lua` runs one script on demand: it is executed in the same
sandbox and its `main(ctx)` (when defined) is called with ctx.event set
to "lua".

When an organism loads, `LuaHost` (`lua_host.py`) owns the single Lua
runtime per organism: classic scripts and `modules/*/init.lua` share one
sandbox, one service registry, and one open event bus (any event name;
`ctx.events:declare(name)` makes a module's custom event first-class, and
module-emitted events also reach matching script `on_<name>` handlers).
Python services (arm, store, persona, visual, commands) are thin
capability bridges: table-in/plain-data-out; failures raise Lua-catchable
errors that module code wraps in pcall."""

import threading
from pathlib import Path

from replicanta import lua_sandbox, telemetry
from replicanta.modules import HookService


class HookEngine:
    """Discovers and fires Lua hooks. Pure apart from the emit sinks (the
    TUI points the live one at the chat log); headless organisms work too."""

    def __init__(self, scripts_dir, emit=None, hooks_service=None, host=None):
        self.scripts_dir = Path(scripts_dir)
        self._live_sink = emit if emit is not None else (lambda _msg: None)
        self._store_sink = None  # installed by the organism: persisted copy of every line
        self.hooks_service = hooks_service
        self._host = None
        self._lock = threading.Lock()
        self._lua = None
        self._available = None  # None = untested, False = lupa missing
        if host is not None:
            self.attach_host(host)
        else:
            self.reload()

    def reload(self):
        """Re-read the scripts directory (drop + rebuild the runtime)."""
        if self._host is not None:
            self._host.reload_scripts()
            self.scripts = self._host.scripts  # keep the mirror fresh for hosted readers
            return
        self.scripts = sorted(self.scripts_dir.glob("*.lua")) if self.scripts_dir.is_dir() else []
        self._lua = None

    # -- emit routing --------------------------------------------------------
    @property
    def emit(self):
        """The live sink: where hook log lines land for the current consumer
        (the TUI points this at the chat log). Assigning to ``emit`` routes
        through set_emit, so ``engine.emit = fn`` keeps working."""
        return self._live_sink

    @emit.setter
    def emit(self, sink):
        self.set_emit(sink)

    def set_emit(self, sink):
        """Point the live sink at ``sink`` (None restores the no-op default).
        The store sink installed via set_store_sink keeps receiving a copy
        of every line, so re-routing the UI never drops persisted history."""
        self._live_sink = sink if sink is not None else (lambda _msg: None)

    def set_store_sink(self, sink):
        """Install (or clear, with None) the persistent recorder that every
        hook log line is fanned out to — the organism points this at the
        belief store's chat log."""
        self._store_sink = sink

    def _route(self, msg):
        """Single owner for a hook log line: fan out to the store + live sink."""
        if self._store_sink is not None:
            self._store_sink(msg)
        self._live_sink(msg)

    def attach_host(self, host):
        """Delegate fire/run/reload to a LuaHost (created later, in
        organism.load()): script discovery and dispatch mirror the host,
        the engine follows the host's event bus, and the host's log lines
        route through this engine's fan — so re-pointing the live sink
        keeps controlling where every script line lands."""
        self._host = host
        self.hooks_service = host.hooks
        host.set_emit(self._route)
        self.reload()

    # -- runtime -----------------------------------------------------------
    def _runtime(self):
        return lua_sandbox.build_runtime()

    # -- firing --------------------------------------------------------------
    def _ensure_runtime(self):
        """Lazily init the Lua runtime. Returns the disabled message when
        lupa is missing (and latches _available False), None on success.
        Caller must hold self._lock."""
        if self._available is False:
            return "lua hooks disabled: the 'lupa' package is not installed"
        try:
            if self._lua is None:
                self._lua = self._runtime()
                self._available = True
        except ImportError:
            self._available = False
            return "lua hooks disabled: the 'lupa' package is not installed"
        return None

    def fire(self, event, org, text=None):
        """Call on_<event>(ctx) in every script. Never raises."""
        if self._host is not None:
            # The host was wired to this engine's _route in attach_host, so
            # its log lines already follow the live sink + store fan.
            self._host.fire(event, org=org, text=text)
            return
        if self.hooks_service is not None:
            self.hooks_service.emit(event, text)
        if not self.scripts or event not in HookService.EVENTS:
            return
        with telemetry.get_tracer(__name__).start_as_current_span("hooks.fire") as span:
            span.set_attribute("hook.event", event)
            span.set_attribute("hook.script_count", len(self.scripts))
            with self._lock:
                was_latched = self._available is False
                disabled = self._ensure_runtime()
                if disabled is not None:
                    if not was_latched:
                        self._route(disabled)
                    return
                try:
                    ctx = (
                        lua_sandbox.build_hook_ctx(self._lua, org, event, text, self._route)
                        if org is not None
                        else self._lua.table(event=event, text=text)
                    )
                except Exception as exc:  # noqa: BLE001 — 'Never raises' covers ctx building too
                    self._route(f"ctx: {exc}")
                    return
                for name, hook in lua_sandbox.collect_script_handlers(self._lua, self.scripts, event, self._route):
                    try:
                        hook(ctx)
                    except Exception as exc:  # noqa: BLE001 — user scripts must never kill the organism
                        self._route(f"{name}: {exc}")

    def run(self, name, org):
        """Run one named script on demand (the /lua command): execute it in
        the shared sandbox, then call its main(ctx) when defined. Returns
        a status line for the chat log; never raises."""
        if self._host is not None:
            return self._host.run(name, org)
        if Path(name).name != name or not name.endswith(".lua"):
            return f"/lua: bad script name {name!r} (want a plain *.lua file)"
        script = self.scripts_dir / name
        if not script.is_file():
            return f"/lua: no {name} in {self.scripts_dir}"
        with self._lock:
            disabled = self._ensure_runtime()
            if disabled is not None:
                return disabled
            try:
                globals_ = self._lua.globals()
                globals_["main"] = None  # a previous /lua run's main must not leak into this script
                lua_sandbox.sandboxed_execute(self._lua, script.read_text(), name=name)
                main = globals_["main"]
                if main is not None:
                    main(lua_sandbox.build_hook_ctx(self._lua, org, "lua", name, self._route))
                return f"lua: ran {name}"
            except Exception as exc:  # noqa: BLE001 — user scripts must never kill the organism
                return f"{name}: {exc}"
            finally:
                self._lua.globals()["main"] = None  # and this run's main must not leak into the next


def scripts_dir_for(dir_path):
    """Where an organism's hooks live: the nursery root's scripts/ when the
    organism is in a nursery (organisms/<name>/), else beside it."""
    dir_path = Path(dir_path)
    if dir_path.parent.name == "organisms":
        return dir_path.parent.parent / "scripts"
    return dir_path / "scripts"
