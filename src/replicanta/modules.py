"""Lua module loader and service registry for Replicanta plugins."""

import logging
import tomllib
from pathlib import Path

from lupa import lua_type

from replicanta import config as project_config
from replicanta import lua_sandbox, rdd, tendon_hand
from replicanta.fileutil import atomic_write_text

logger = logging.getLogger(__name__)


class ServiceRegistry:
    """Simple key/value service registry for modules."""

    def __init__(self):
        self._services = {}

    def register(self, name, service):
        self._services[name] = service

    def get(self, name):
        return self._services.get(name)


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
    when the handler is a lupa function.
    """

    def __init__(self, loader=None):
        self._commands = {}
        self._loader = loader

    def register(self, name, handler):
        self._commands[name] = handler

    def _table_from(self, args):
        if self._loader is None:
            raise RuntimeError("Lua command handler without the module loader's Lua runtime")
        return self._loader._runtime().table_from(args)

    def dispatch(self, name, args):
        handler = self._commands.get(name)
        if handler is None:
            return None
        if lua_type(handler) == "function":
            return handler(self._table_from(args))
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
        config=None,
        persona_config=None,
        host=None,
    ):
        self.modules_dir = Path(modules_dir)
        self.organism = organism
        if modules_config is not None:
            self.modules_config = modules_config
        elif config is not None:
            self.modules_config = config.get("modules", {})
        else:
            self.modules_config = {}
        # Backward-compatible alias for callers that read loader.config.
        self.config = self.modules_config
        if persona_config is not None:
            self.persona_config = persona_config
        elif config is not None:
            self.persona_config = config.get("persona", {})
        else:
            self.persona_config = {}
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
            ]
        enabled = set(enabled)
        discovered = [m for m in discovered if m.get("name") in enabled]
        ordered = self._resolve_load_order(discovered)
        for manifest in ordered:
            self._init_module(manifest)

    def _register_builtin_services(self):
        self.registry.register("organism", self.organism)
        self.registry.register(
            "store",
            _StoreService(self.organism.store) if self.organism else None,
        )
        # Hosted loaders share the host's open bus so module subscriptions
        # made during init land on the same bus dispatch emits through.
        self.registry.register(
            "hooks",
            self._host.hooks if self._host is not None else HookService(),
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
        try:
            lua = self._runtime()
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
        self.modules[name] = manifest

    def _runtime(self):
        if self._host is not None:
            return self._host.lua
        if self._lua is None:
            self._lua = lua_sandbox.build_runtime()
        return self._lua

    def _build_context(self, module_name):
        lua = self._runtime()
        return lua.table(
            module_name=module_name,
            log=lambda msg: self.emit(str(msg)),
            services=self.registry,
            events=(self._host.events if self._host is not None else self.registry.get("hooks")),
        )


class _StoreService:
    def __init__(self, store):
        self.store = store

    def observe(self, belief, conf):
        self.store.observe(belief, conf)


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

    def __init__(self, store, persona_config=None, root=None, config=None):
        self.store = store
        if persona_config is not None:
            self.persona_config = persona_config
        elif config is not None:
            self.persona_config = config.setdefault("persona", {})
        else:
            self.persona_config = {}
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
