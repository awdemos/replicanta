"""Lua module loader and service registry for Replicanta plugins."""

import logging
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

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
    """Discovers Lua modules from a directory."""

    def __init__(self, modules_dir, organism=None, config=None, emit=None):
        self.modules_dir = Path(modules_dir)
        self.organism = organism
        self.config = config or {}
        self.emit = emit if emit is not None else (lambda _msg: None)
        self.registry = ServiceRegistry()
        self.modules = {}
        self.warnings = []

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
