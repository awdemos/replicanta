from pathlib import Path

import pytest

from replicanta.modules import ModuleLoader, ServiceRegistry


def test_service_registry_register_and_get():
    reg = ServiceRegistry()
    reg.register("foo", "bar")
    assert reg.get("foo") == "bar"


def test_service_registry_missing_returns_none():
    reg = ServiceRegistry()
    assert reg.get("missing") is None


def test_module_loader_discovers_valid_modules(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "manifest.toml").write_text(
        'name = "alpha"\nversion = "1.0.0"\n'
    )
    (tmp_path / "alpha" / "init.lua").write_text("function init(ctx) end\n")
    loader = ModuleLoader(tmp_path, organism=None, config={})
    modules = loader._discover()
    assert len(modules) == 1
    assert modules[0]["name"] == "alpha"
