"""LuaHost: one Lua runtime per organism hosting scripts and modules.

Owns the hardened runtime, the service registry, the open event bus
(HookService), and dispatch. HookEngine and ModuleLoader delegate here when
attached via their ``host`` parameter; standalone construction still works.
"""

import threading
from pathlib import Path

from replicanta import lua_sandbox, telemetry
from replicanta.modules import HookService, ModuleLoader, ServiceRegistry


class LuaHost:
    """Single runtime + registry + event bus for one organism."""

    def __init__(self, scripts_dir=None, modules_dir=None, organism=None, emit=None, root=None):
        self.emit = emit if emit is not None else (lambda _msg: None)
        self.lock = threading.RLock()  # reentrant: module handlers may emit events
        self.lua = lua_sandbox.build_runtime()
        self.registry = ServiceRegistry()
        self.hooks = HookService(on_error=lambda msg: self.emit(msg))
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

        The loader registers its builtin services; the host's event bus is
        the registry's ``hooks`` entry (the loader adopts it when hosted), so
        ``services.get`` and ``ctx.events`` see the same objects.
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
