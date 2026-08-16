from replicanta.modules import (
    ModuleLoader,
    ServiceRegistry,
)


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


def test_resolve_load_order_linear(tmp_path):
    for name in ("base", "derived"):
        d = tmp_path / name
        d.mkdir()
        (d / "manifest.toml").write_text(
            f'name = "{name}"\nversion = "1.0.0"\n'
        )
    (tmp_path / "derived" / "manifest.toml").write_text(
        'name = "derived"\nversion = "1.0.0"\ndepends = ["base"]\n'
    )
    loader = ModuleLoader(tmp_path, organism=None, config={})
    modules = loader._discover()
    ordered = loader._resolve_load_order(modules)
    assert [m["name"] for m in ordered] == ["base", "derived"]


def test_resolve_load_order_missing_dependency(tmp_path):
    d = tmp_path / "orphan"
    d.mkdir()
    (d / "manifest.toml").write_text(
        'name = "orphan"\nversion = "1.0.0"\ndepends = ["missing"]\n'
    )
    loader = ModuleLoader(tmp_path, organism=None, config={})
    modules = loader._discover()
    ordered = loader._resolve_load_order(modules)
    assert ordered == []
    assert any("missing" in w for w in loader.warnings)


def test_resolve_load_order_circular(tmp_path):
    for name in ("a", "b"):
        d = tmp_path / name
        d.mkdir()
        (d / "manifest.toml").write_text(
            f'name = "{name}"\nversion = "1.0.0"\ndepends = ["{"b" if name == "a" else "a"}"]\n'
        )
    loader = ModuleLoader(tmp_path, organism=None, config={})
    modules = loader._discover()
    ordered = loader._resolve_load_order(modules)
    assert ordered == []
    assert any("circular" in w.lower() for w in loader.warnings)


def test_resolve_load_order_invalid_depends_type(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "manifest.toml").write_text(
        'name = "bad"\nversion = "1.0.0"\ndepends = "base"\n'
    )
    loader = ModuleLoader(tmp_path, organism=None, config={})
    modules = loader._discover()
    ordered = loader._resolve_load_order(modules)
    assert [m["name"] for m in ordered] == ["bad"]
    assert any("depends must be a list" in w for w in loader.warnings)


def test_load_all_initializes_modules(tmp_path):
    d = tmp_path / "cmdmod"
    d.mkdir()
    (d / "manifest.toml").write_text('name = "cmdmod"\nversion = "1.0.0"\n')
    (d / "init.lua").write_text(
        'function init(ctx)\n'
        '  ctx.services.get("commands"):register("/hello", function(args) return "hi" end)\n'
        'end\n'
    )
    loader = ModuleLoader(tmp_path, organism=None, config={"modules": {"enabled": ["cmdmod"]}})
    loader.load_all()
    result = loader.registry.get("commands").dispatch("/hello", [])
    assert result == "hi"


def test_load_all_empty_whitelist_loads_none(tmp_path):
    d = tmp_path / "cmdmod"
    d.mkdir()
    (d / "manifest.toml").write_text('name = "cmdmod"\nversion = "1.0.0"\n')
    (d / "init.lua").write_text('function init(ctx) end\n')
    loader = ModuleLoader(tmp_path, organism=None, config={"modules": {"enabled": []}})
    loader.load_all()
    assert loader.modules == {}


def test_lua_sandbox_blocks_dangerous_globals(tmp_path):
    d = tmp_path / "sandbox"
    d.mkdir()
    (d / "manifest.toml").write_text('name = "sandbox"\nversion = "1.0.0"\n')
    blocked = ["os", "io", "load", "loadfile", "dofile", "require", "package", "debug"]
    checks = []
    for name in blocked:
        checks.append(f'if {name} ~= nil then error("{name} not blocked") end')
    lua_code = 'function init(ctx)\n' + '\n'.join('  ' + c for c in checks) + '\nend\n'
    (d / "init.lua").write_text(lua_code)
    loader = ModuleLoader(tmp_path, organism=None, config={"modules": {"enabled": ["sandbox"]}})
    loader.load_all()
    assert "sandbox" in loader.modules
    assert not any("sandbox" in w for w in loader.warnings)
