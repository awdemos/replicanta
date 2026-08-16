"""Lua module loader and service registry for Replicanta plugins."""

import logging
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from lupa import LuaRuntime

from replicanta import config as project_config

_BLOCKED_GLOBALS = ("os", "io", "load", "loadfile", "loadstring", "require", "dofile", "package", "debug")

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
    """Event bus used by modules and consumed by HookEngine."""

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

    def on(self, event, handler):
        if event not in self._handlers:
            logger.warning("unknown hook event: %s", event)
            return
        self._handlers[event].append(handler)

    def emit(self, event, text=None):
        if event not in self._handlers:
            return
        for handler in self._handlers[event]:
            try:
                handler(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hook handler for %s failed: %s", event, exc)


class CommandService:
    """Slash-command registry used by modules and consumed by the TUI."""

    def __init__(self):
        self._commands = {}

    def register(self, name, handler):
        self._commands[name] = handler

    def dispatch(self, name, args):
        handler = self._commands.get(name)
        if handler is None:
            return None
        return handler(args)


class ModuleLoader:
    """Discovers and initializes Lua modules from a directory."""

    def __init__(self, modules_dir, organism=None, config=None, emit=None, root=None):
        self.modules_dir = Path(modules_dir)
        self.organism = organism
        self.config = config or {}
        self.emit = emit if emit is not None else (lambda _msg: None)
        self.root = root
        self.registry = ServiceRegistry()
        self.modules = {}
        self.warnings = []
        self._lua = None

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
                self.warnings.append(
                    f"circular dependency detected: {' -> '.join(path + [name])}"
                )
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
        self._register_builtin_services()
        discovered = self._discover()
        enabled = self.config.get("modules", {}).get("enabled")
        if enabled is None:
            enabled = [m.get("name") for m in discovered]
        enabled = set(enabled)
        if enabled:
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
        self.registry.register("hooks", HookService())
        self.registry.register("commands", CommandService())
        self.registry.register(
            "persona",
            PersonaService(
                self.organism.store if self.organism else None,
                config=self.config,
                root=self.root,
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
            lua.execute(init_path.read_text())
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
        if self._lua is None:
            self._lua = LuaRuntime(register_eval=False, register_builtins=False)
            for name in _BLOCKED_GLOBALS:
                self._lua.execute(f"{name} = nil")
        return self._lua

    def _build_context(self, module_name):
        lua = self._runtime()
        return lua.table(
            module_name=module_name,
            log=lambda msg: self.emit(str(msg)),
            services=self.registry,
        )


class _StoreService:
    def __init__(self, store):
        self.store = store

    def observe(self, belief, conf):
        self.store.observe(belief, conf)


class PersonaService:
    """Registry and activation for persona modules."""

    def __init__(self, store, config=None, root=None):
        self.store = store
        self.config = config if config is not None else {}
        self.root = root
        self._personas = {}

    def register(self, spec):
        name = spec.get("name")
        if not name:
            logger.warning("persona spec missing name; skipping")
            return
        self._personas[name] = spec

    def list(self):
        return sorted(self._personas)

    def active(self):
        active = self.config.get("persona", {}).get("active")
        return self._personas.get(active)

    def prompt_fragment(self):
        spec = self.active()
        return spec.get("prompt", "") if spec else ""

    def _save(self):
        if self.root is not None:
            project_config.save_config(self.root, self.config)

    def activate(self, name):
        spec = self._personas.get(name)
        if spec is None:
            logger.warning("unknown persona: %s", name)
            return
        self.config.setdefault("persona", {})["active"] = name
        for belief in spec.get("beliefs", []):
            if len(belief) == 3:
                self.store.observe(tuple(belief), 0.9)
        self.store.remember("persona", f"adopted the {name} persona")
        self._save()

    def deactivate(self):
        self.config.setdefault("persona", {})["active"] = ""
        self._save()
