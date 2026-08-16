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


class ModuleLoader:
    """Discovers and initializes Lua modules from a directory."""

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
            manifest["_dir"] = path
            manifest["_init_path"] = init_path
            found.append(manifest)
        return found
