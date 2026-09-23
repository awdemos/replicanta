"""Lua module loader and service registry for Replicanta plugins."""

import logging
import re
import time
import tomllib
from pathlib import Path

from lupa import lua_type

from replicanta import capbridges, config as project_config, externals
from replicanta import lua_sandbox, rdd, tendon_hand
from replicanta.fileutil import atomic_write_text

logger = logging.getLogger(__name__)


class ServiceRegistry:
    """Simple key/value service registry for modules.

    Each registry owns an ``owner`` token (itself unless overridden); the
    per-module process bridges are tagged with it so shutdown() reaps only
    THIS registry's children — one organism's module reload must not kill
    another organism's games.
    """

    def __init__(self, owner_token=None):
        self._services = {}
        self._owner = owner_token if owner_token is not None else self
        self._shutdown = False

    def register(self, name, service):
        self._services[name] = service

    def get(self, name):
        return self._services.get(name)

    def shutdown(self):
        """Retire every service and reap this registry's child processes.

        Idempotent: safe to call twice (Organism.close() and a following
        module reload may both reach the same registry). For each service
        an optional ``shutdown()`` method is preferred, then ``stop()`` —
        so the arm bridge's SSE/volition threads retire instead of leaking
        and competing with their replacements — and finally the owned
        process children are killed via capbridges.shutdown_all(owner=...).
        """
        if self._shutdown:
            return
        self._shutdown = True
        for name, service in list(self._services.items()):
            for method in ("shutdown", "stop"):
                fn = getattr(service, method, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("service %s failed to %s: %s", name, method, exc)
                    break
        capbridges.shutdown_all(owner=self._owner)


class CallService:
    """ctx.call: one tolerant parser/router for ``svc.method("arg", 2)``
    lines written by the entity in its own output.

    Routes to the loader's CURRENT registry (it is replaced on reload, so
    the service is resolved per call, not cached). Returns
    ``result, nil`` on success and ``nil, "message"`` for anything else —
    unknown shapes, unknown services/methods, and handler errors — so Lua
    modules can branch or pcall without a bespoke parser each.
    """

    _CALL_PARENS = re.compile(r"^\s*([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\((.*)\)\s*$", re.DOTALL)
    _CALL_SUGAR = re.compile(r"""^\s*([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*["'](.*)["']\s*$""")

    def __init__(self, registry):
        self._registry = registry

    def parse(self, line):
        """(service, method, *args) for one call-shaped line, else None.
        Tolerates ``svc.method("a", 2)``, ``svc.method(a)`` (bare single
        args become strings), and the Lua-call sugar ``svc.method "a"``.
        The flat shape lets Lua pack the returns into a table:
        ``local parts = {ctx.call.parse(line)}``."""
        text = str(line or "").strip()
        match = self._CALL_PARENS.match(text)
        if match is not None:
            arg_text = match.group(3)
        else:
            match = self._CALL_SUGAR.match(text)
            if match is None:
                return None
            arg_text = repr(match.group(3))
        service, method = match.group(1), match.group(2)
        if service.startswith("_") or method.startswith("_"):
            return None
        return service, method, *self._split_args(arg_text)

    @staticmethod
    def _split_args(arg_text):
        args = []
        current = []
        quote = None
        for ch in str(arg_text):
            if quote is not None:
                current.append(ch)
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
                current.append(ch)
            elif ch == ",":
                args.append("".join(current).strip())
                current = []
            else:
                current.append(ch)
        tail = "".join(current).strip()
        if tail or args:
            args.append(tail)
        return [CallService._coerce(a) for a in args if a != ""]

    @staticmethod
    def _coerce(token):
        if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
            return token[1:-1]
        lowered = token.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in ("nil", "null"):
            return None
        try:
            return int(token)
        except ValueError:
            pass
        try:
            return float(token)
        except ValueError:
            pass
        return token  # bare word (e.g. quote-less doom.command(shoot))

    def __call__(self, line):
        """Parse and execute one call line. Returns (result, err)."""
        parsed = self.parse(line)
        if parsed is None:
            return None, "not a service call"
        service_name, method, *args = parsed
        service = self._registry.get(service_name)
        if service is None:
            return None, f"unknown service '{service_name}'"
        fn = getattr(service, method, None)
        if fn is None or not callable(fn):
            return None, f"unknown method '{service_name}.{method}'"
        try:
            return fn(*args), None
        except Exception as exc:  # noqa: BLE001 — handler errors are data here
            return None, str(exc)


class OrganismFacade:
    """Narrow, Lua-safe view of the organism, registered as the
    ``organism`` service.

    The registry used to hand sandboxed Lua the live Organism object whose
    public Path attributes (dir_path and the store's paths) let a module
    walk the host filesystem — subscript/parent chains bypass the sandbox's
    attribute filter. This facade exposes ONLY what modules consume, as
    methods returning plain data (strings/bools, never Paths); everything
    else is unreachable.
    """

    def __init__(self, organism):
        self._organism = organism

    def name(self):
        """The organism's directory name (a string, never a Path)."""
        return self._organism.dir_path.name

    def entity_actuation(self):
        """The persisted entity-actuation toggle (default True). When false,
        entity-initiated physical/process actions (doom moves, hand moves,
        brain runs dispatched from utterance hooks) must not execute.
        Defensive getattr: organisms without the attribute (older saves,
        test doubles) default to enabled."""
        return bool(getattr(self._organism, "entity_actuation", True))


class HookService:
    """Open event bus used by modules and consumed by HookEngine/LuaHost.

    Core lifecycle events (EVENTS) are always valid; modules may additionally
    declare custom event names (declare) and emit/subscribe any string.
    Undeclared emits/subscribes work but log at debug level so typos on core
    events are catchable without blocking dynamism. Handler failures are
    logged at warning level and additionally reported through ``on_error``
    (one callable taking a message string) when one is provided.
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

    def __init__(self, on_error=None):
        self._handlers = {e: [] for e in self.EVENTS}
        self._declared = set(self.EVENTS)
        self._on_error = on_error

    def declare(self, name):
        """Register a first-class event name (idempotent)."""
        self._declared.add(str(name))

    def known(self):
        """All event names currently known to the bus."""
        return sorted(self._declared)

    def on(self, event, handler):
        event = str(event)
        if event not in self._declared:
            logger.debug("undeclared hook event subscribed: %s", event)
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event, text=None):
        event = str(event)
        if event not in self._declared:
            logger.debug("undeclared hook event emitted: %s", event)
        for handler in self._handlers.get(event, []):
            try:
                handler(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hook handler for %s failed: %s", event, exc)
                if self._on_error is not None:
                    self._on_error(f"hook handler for {event} failed: {exc}")


class CommandService:
    """Slash-command registry used by modules and consumed by the TUI.

    Handlers registered from Lua receive args as a Lua table, while callers
    pass plain Python lists. Dispatch coerces the list via the Lua runtime
    that originally registered the handler so modules can safely use their
    own Lua runtimes without cross-runtime object leakage.
    """

    def __init__(self, loader=None):
        self._commands = {}
        self._loader = loader

    def register(self, name, handler):
        """Store the handler along with the Lua runtime it belongs to."""
        runtime = None
        if self._loader is not None and lua_type(handler) == "function":
            runtime = getattr(self._loader, "_current_lua", None)
        self._commands[name] = (handler, runtime)

    def has(self, name):
        """True when a handler is registered for ``name``.

        Lets dispatch callers distinguish an unknown command from a
        handler that ran and returned nothing."""
        return name in self._commands

    def _table_from(self, runtime, args):
        if runtime is None:
            if self._loader is None:
                raise RuntimeError("Lua command handler without the module loader's Lua runtime")
            runtime = self._loader._runtime()
        return runtime.table_from(args)

    def dispatch(self, name, args):
        item = self._commands.get(name)
        if item is None:
            return None
        handler, runtime = item
        if lua_type(handler) == "function":
            return handler(self._table_from(runtime, args))
        return handler(args)


class ModuleLoader:
    """Discovers and initializes Lua modules from a directory."""

    def __init__(
        self,
        modules_dir,
        organism=None,
        modules_config=None,
        emit=None,
        root=None,
        persona_config=None,
        host=None,
    ):
        self.modules_dir = Path(modules_dir)
        self.organism = organism
        self.modules_config = modules_config if modules_config is not None else {}
        self.persona_config = persona_config if persona_config is not None else {}
        self.emit = emit if emit is not None else (lambda _msg: None)
        self.root = root
        self.registry = ServiceRegistry()
        self.modules = {}
        self.warnings = []
        self._lua = None
        self._host = host  # LuaHost: share its runtime, bus, and lock

    def _discover(self):
        """Return list of manifest dicts for modules under modules_dir."""
        found = []
        if not self.modules_dir.is_dir():
            return found
        for path in sorted(self.modules_dir.iterdir()):
            manifest_path = path / "manifest.toml"
            init_path = path / "init.lua"
            if not manifest_path.is_file():
                continue
            try:
                manifest = tomllib.loads(manifest_path.read_text())
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"{path.name}: malformed manifest: {exc}")
                continue
            if not isinstance(manifest, dict):
                self.warnings.append(f"{path.name}: manifest is not a table")
                continue
            if not manifest.get("name"):
                self.warnings.append(f"{path.name}: manifest missing name; skipping")
                continue
            manifest["_dir"] = path
            manifest["_init_path"] = init_path
            found.append(manifest)
        return found

    def _resolve_load_order(self, modules):
        """Topological sort by depends. Returns ordered list; logs warnings
        and returns [] on missing deps or cycles."""
        by_name = {}
        for m in modules:
            name = m.get("name")
            if not name:
                continue
            if name in by_name:
                self.warnings.append(f"duplicate module name '{name}'; keeping last")
            by_name[name] = m
        ordered = []
        visited = set()
        temp = set()

        def visit(name, path):
            if name in temp:
                self.warnings.append(f"circular dependency detected: {' -> '.join(path + [name])}")
                return False
            if name in visited:
                return True
            if name not in by_name:
                self.warnings.append(f"dependency '{name}' not found; aborting load")
                return False
            temp.add(name)
            depends = by_name[name].get("depends", [])
            if not isinstance(depends, list):
                self.warnings.append(f"{name}: depends must be a list; skipping")
                depends = []
            for dep in depends:
                if not visit(dep, path + [name]):
                    return False
            temp.remove(name)
            visited.add(name)
            ordered.append(by_name[name])
            return True

        for name in sorted(by_name):
            if name not in visited and not visit(name, []):
                return []
        return ordered

    def load_all(self):
        """Discover, resolve, and initialize all enabled modules."""
        # Retire the previous load's services first (arm SSE/volition threads,
        # process children): the registry is owner-scoped, so only THIS
        # loader's children are reaped — other organisms' games survive.
        self.registry.shutdown()
        self.registry = ServiceRegistry()
        self.modules = {}
        self.warnings = []
        self._lua = None  # reset so _register_builtin_services uses the right runtime
        self._register_builtin_services()
        discovered = self._discover()
        enabled = self.modules_config.get("enabled")
        if enabled is None:
            enabled = [
                "base",
                "software-engineer",
                "creative-writer",
                "socratic-philosopher",
                "visual-state",
                "tendon-hand",
                "fly-brain",
                "doom-ascii",
            ]
        # Config alias: the engine swap renamed nano-doom to doom-ascii;
        # existing organism configs that enable "nano-doom" keep working.
        enabled = {"doom-ascii" if name == "nano-doom" else name for name in enabled}
        discovered = [m for m in discovered if m.get("name") in enabled]
        ordered = self._resolve_load_order(discovered)
        for manifest in ordered:
            self._init_module(manifest)

    def _register_builtin_services(self):
        # The facade, never the live Organism: its public Path attributes
        # would let sandboxed Lua walk the host filesystem (parent/subscript
        # chains bypass the sandbox attribute filter). None when no organism.
        self.registry.register(
            "organism",
            OrganismFacade(self.organism) if self.organism is not None else None,
        )
        self.registry.register(
            "store",
            _StoreService(self.organism.store) if self.organism else None,
        )
        # Hosted loaders register the host's events facade so services.get
        # and ctx.events are the same object: declare/on/known delegate to
        # the shared bus, and emit gains the host's script broadcast.
        self.registry.register(
            "hooks",
            self._host.events if self._host is not None else HookService(),
        )
        self.registry.register("commands", CommandService(self))
        self.registry.register(
            "persona",
            PersonaService(
                self.organism.store if self.organism else None,
                persona_config=self.persona_config,
                root=self.root,
            ),
        )
        self.registry.register(
            "visual",
            VisualService(self.organism),
        )
        self.registry.register(
            "externals",
            externals.ExternalsService(root=self.root),
        )
        self.registry.register(
            "arm",
            tendon_hand.ArmService(
                self.organism,
                lua_lock=(self._host.lock if self._host is not None else None),
            ),
        )

    def _init_module(self, manifest):
        name = manifest.get("name")
        init_path = manifest.get("_init_path")
        if not init_path.is_file():
            self.warnings.append(f"{name}: init.lua missing; skipping")
            return
        # Manifests may carry unknown keys (services = [...] was retired);
        # parsing stays tolerant and they are simply ignored.
        try:
            lua = self._runtime()
            # Track the runtime that will own this module's Lua callbacks
            # (command handlers, event subscriptions) so dispatch can build
            # arguments in the correct runtime and avoid cross-runtime leaks.
            self._current_lua = lua
            lua_sandbox.sandboxed_execute(lua, init_path.read_text(), name=name)
            init = lua.globals()["init"]
            if init is None:
                self.warnings.append(f"{name}: no init() function; skipping")
                return
            ctx = self._build_context(name)
            init(ctx)
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"{name}: init failed: {exc}")
            return
        finally:
            self._current_lua = None
        self.modules[name] = manifest

    def _runtime(self):
        # Each module gets its own hardened Lua runtime.  Sharing a runtime
        # across modules caused lupa to confuse local variables / attribute
        # lookups after several modules loaded, breaking later modules (e.g.
        # doom-ascii failing with "'DoomAsciiService' object has no attribute
        # 'register'").  Per-module runtimes avoid that state leakage.
        return lua_sandbox.build_runtime()

    def _build_context(self, module_name):
        # Use the runtime assigned to the module currently being initialised
        # so the context table lives in the same Lua world as the init()
        # function that receives it.
        lua = self._current_lua or self._runtime()
        store = getattr(self.organism, "store", None)
        organism_dir = getattr(store, "dir_path", None)
        bridges = capbridges.build(organism_dir=organism_dir, emit=self.emit, owner=self.registry)

        def invoke_lua(fn):
            # Timer callbacks run on timer threads, off the UI thread: enter
            # the module's own runtime with pcall containment so a raising
            # handler becomes one emitted line instead of a dead timer.
            try:
                ok, err = lua.eval(
                    "function(f) local ok, err = pcall(f);"
                    " if not ok then return false, tostring(err) end return true end"
                )(fn)
                if ok is not True:
                    self.emit(f"{module_name}: timer handler failed: {err}")
            except Exception as exc:  # noqa: BLE001 — runtime gone etc.
                self.emit(f"{module_name}: timer handler failed: {exc}")

        timers = capbridges.TimerBridge(invoke_lua, emit=self.emit)
        kv_path = Path(organism_dir) / "kv" / f"{module_name}.json" if organism_dir else None
        kv_bridge = capbridges.KvBridge(kv_path, emit=self.emit)
        # A Lua-table facade so colon calls (ctx.kv:get("k")) receive self
        # natively; keys() returns a real Lua table (Python lists cross as
        # opaque userdata, so it is built in the module's runtime).
        kv = lua.eval(
            "function(py, keys_table)\n"
            "  local t = {}\n"
            "  function t.get(self, key, default)\n"
            "    if default == nil then return py.get(key) end\n"
            "    return py.get(key, default)\n"
            "  end\n"
            "  function t.set(self, key, value) return py.set(key, value) end\n"
            "  function t.delete(self, key) return py.delete(key) end\n"
            "  function t.keys(self) return keys_table() end\n"
            "  return t\n"
            "end"
        )(kv_bridge, lambda: lua.table_from(kv_bridge.keys()))
        return lua.table(
            module_name=module_name,
            log=lambda msg: self.emit(str(msg)),
            services=self.registry,
            events=(self._host.events if self._host is not None else self.registry.get("hooks")),
            process=bridges.process,
            http=bridges.http,
            fs=bridges.fs,
            json=bridges.json,
            after=timers.after,
            every=timers.every,
            kv=kv,
            call=CallService(self.registry),
            clock=time.monotonic,
        )


class _StoreService:
    """Narrow store facade: observation/memory verbs only. The live
    BeliefStore stays private — exposing it handed Lua Path attributes
    (dir_path) and every store method, far more surface than modules use."""

    def __init__(self, store):
        self._store = store

    def observe(self, belief, conf):
        self._store.observe(belief, conf)

    def remember(self, kind, text):
        self._store.remember(str(kind), str(text))


class VisualService:
    """Render RDD lineage and charts for the organism's state.

    Exposed to Lua modules as services.get('visual').build(kind) returns a
    table with text_chart, path, and SVG files written to artifacts/.
    """

    def __init__(self, organism):
        self.organism = organism

    def build(self, kind="beliefs"):
        if self.organism is None:
            raise RuntimeError("no organism")
        result = rdd.build_chart(kind, self.organism.store)
        artifacts = self.organism.store.dir_path / "artifacts" / "visual-state"
        artifacts.mkdir(parents=True, exist_ok=True)
        lineage_path = artifacts / f"{kind}-lineage.svg"
        chart_path = artifacts / f"{kind}-chart.svg"
        atomic_write_text(lineage_path, result["lineage_svg"])
        atomic_write_text(chart_path, result["chart_svg"])

        # Return a plain object whose attributes Lua can access via attribute
        # handlers; lupa does not expose Python dict keys as table pairs.
        class Result:
            text_chart: str
            path: str
            lineage_path: str
            kind: str
            caption: str
            records: list

        out = Result()
        out.text_chart = result["text_chart"]
        out.path = str(chart_path)
        out.lineage_path = str(lineage_path)
        out.kind = result["kind"]
        out.caption = result.get("caption", "")
        out.records = result["records"]
        return out


class PersonaService:
    """Registry and activation for persona modules."""

    def __init__(self, store, persona_config=None, root=None):
        self.store = store
        self.persona_config = persona_config if persona_config is not None else {}
        self.root = root
        self._personas = {}

    def register(self, spec):
        spec = lua_sandbox.to_py(spec)
        name = spec.get("name")
        if not name:
            logger.warning("persona spec missing name; skipping")
            return
        self._personas[name] = spec

    def list(self):
        return sorted(self._personas)

    def active(self):
        active = self.persona_config.get("active")
        return self._personas.get(active)

    def prompt_fragment(self):
        spec = self.active()
        return spec.get("prompt", "") if spec else ""

    def _save(self):
        if self.root is not None:
            cfg = project_config.load_config(self.root)
            cfg["persona"] = self.persona_config
            project_config.save_config(self.root, cfg)

    def activate(self, name):
        spec = self._personas.get(name)
        if spec is None:
            logger.warning("unknown persona: %s", name)
            return
        self.persona_config["active"] = name
        if self.store is not None:
            for belief in spec.get("beliefs", []):
                if len(belief) == 3:
                    self.store.observe(tuple(belief), 0.9)
            self.store.remember("persona", f"adopted the {name} persona")
        self._save()

    def deactivate(self):
        self.persona_config["active"] = ""
        self._save()
