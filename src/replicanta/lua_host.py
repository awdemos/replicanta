"""LuaHost: one Lua runtime per organism hosting scripts and modules.

Owns the hardened runtime, the service registry, the open event bus
(HookService), and dispatch. HookEngine and ModuleLoader delegate here when
attached via their ``host`` parameter; standalone construction still works.
"""

import threading
from pathlib import Path

from replicanta import lua_sandbox, telemetry
from replicanta.modules import HookService, ModuleLoader


class _LuaEventsFacade:
    """Lua-facing view of the open event bus.

    declare/on/known delegate to the shared bus; emit routes through
    LuaHost.fire so module-emitted events reach BOTH module subscribers
    and classic script on_<name> handlers (the spec's broadcast semantics).
    """

    def __init__(self, host):
        self._host = host

    def declare(self, name):
        self._host.hooks.declare(name)

    def known(self):
        return self._host.hooks.known()

    def on(self, event, handler):
        self._host.hooks.on(event, handler)

    def emit(self, event, text=None):
        self._host.fire(event, org=self._host.organism, text=text)


class LuaHost:
    """Single runtime + registry + event bus for one organism.

    Modules must keep their Lua in locals/closures: the runtime's global
    table is shared with classic scripts, which own the ``on_*`` globals by
    design. A module that writes globals can shadow script state (and a
    stray global ``main`` would be picked up by ``run``).
    """

    def __init__(self, scripts_dir=None, modules_dir=None, organism=None, emit=None, root=None):
        # Once a HookEngine attaches, its fire/run mirror the engine's emit
        # onto this attribute — direct assignment here is stomped on the next
        # delegated call.
        self.emit = emit if emit is not None else (lambda _msg: None)
        self.lock = threading.RLock()  # reentrant: module handlers may emit events
        self.lua = lua_sandbox.build_runtime()
        self.registry = None  # adopted from the loader in load_modules()
        self.hooks = HookService(on_error=lambda msg: self.emit(msg))
        self.events = _LuaEventsFacade(self)
        self.organism = organism
        self.root = root
        self.scripts_dir = Path(scripts_dir) if scripts_dir else None
        self.modules_dir = Path(modules_dir) if modules_dir else None
        self.scripts = []
        self.loader = None

    # -- loading -------------------------------------------------------------
    def reload_scripts(self):
        """Re-read the scripts directory (scripts share the host global table)."""
        self.scripts = sorted(self.scripts_dir.glob("*.lua")) if self.scripts_dir and self.scripts_dir.is_dir() else []

    def load_modules(self, modules_config=None, persona_config=None):
        """Load modules through the facade loader on the host runtime.

        The loader registers its builtin services, including the host's
        events facade as the registry's ``hooks`` entry when hosted (see
        ModuleLoader._register_builtin_services), so ``services.get`` and
        ``ctx.events`` see the same objects.
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
        self.registry = self.loader.registry

    def reload_modules(self, modules_config=None, persona_config=None):
        """Re-init modules on a FRESH event bus.

        ModuleLoader.load_all() alone resets the loader registry but keeps
        the host bus stable, so every module re-init re-subscribes on top of
        the old closures (N reloads -> handlers fire N times). Replacing
        self.hooks first makes the old bus and its subscriptions garbage;
        the _LuaEventsFacade delegates through self._host.hooks at call
        time, so it picks up the new bus automatically.
        """
        self.hooks = HookService(on_error=lambda msg: self.emit(msg))
        self.load_modules(modules_config=modules_config, persona_config=persona_config)
        if self.organism is not None:
            if hasattr(self.organism, "persona_service"):
                self.organism.persona_service = self.registry.get("persona")
            if hasattr(self.organism, "module_loader"):
                self.organism.module_loader = self.loader
            hooks_engine = getattr(self.organism, "hooks", None)
            if hooks_engine is not None:
                hooks_engine.hooks_service = self.hooks

    # -- dispatch ------------------------------------------------------------
    def fire(self, event, org=None, text=None):
        """Emit to module subscribers, then classic script on_<event> handlers.

        Unlike standalone HookEngine, ANY event name dispatches here, so
        module-declared events also reach matching script on_<event> fns.
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
                    ctx = self.lua.table(event=event, text=text, log=lambda msg: self.emit(str(msg)))
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
                    main(
                        self._ctx(org, "lua", name)
                        if org is not None
                        else self.lua.table(event="lua", text=name, log=lambda msg: self.emit(str(msg)))
                    )
                return f"lua: ran {name}"
            except Exception as exc:  # noqa: BLE001
                return f"{name}: {exc}"
