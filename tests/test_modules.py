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
    (tmp_path / "alpha" / "manifest.toml").write_text('name = "alpha"\nversion = "1.0.0"\n')
    (tmp_path / "alpha" / "init.lua").write_text("function init(ctx) end\n")
    loader = ModuleLoader(tmp_path, organism=None, modules_config={})
    modules = loader._discover()
    assert len(modules) == 1
    assert modules[0]["name"] == "alpha"


def test_resolve_load_order_linear(tmp_path):
    for name in ("base", "derived"):
        d = tmp_path / name
        d.mkdir()
        (d / "manifest.toml").write_text(f'name = "{name}"\nversion = "1.0.0"\n')
    (tmp_path / "derived" / "manifest.toml").write_text('name = "derived"\nversion = "1.0.0"\ndepends = ["base"]\n')
    loader = ModuleLoader(tmp_path, organism=None, modules_config={})
    modules = loader._discover()
    ordered = loader._resolve_load_order(modules)
    assert [m["name"] for m in ordered] == ["base", "derived"]


def test_resolve_load_order_missing_dependency(tmp_path):
    d = tmp_path / "orphan"
    d.mkdir()
    (d / "manifest.toml").write_text('name = "orphan"\nversion = "1.0.0"\ndepends = ["missing"]\n')
    loader = ModuleLoader(tmp_path, organism=None, modules_config={})
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
    loader = ModuleLoader(tmp_path, organism=None, modules_config={})
    modules = loader._discover()
    ordered = loader._resolve_load_order(modules)
    assert ordered == []
    assert any("circular" in w.lower() for w in loader.warnings)


def test_resolve_load_order_invalid_depends_type(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "manifest.toml").write_text('name = "bad"\nversion = "1.0.0"\ndepends = "base"\n')
    loader = ModuleLoader(tmp_path, organism=None, modules_config={})
    modules = loader._discover()
    ordered = loader._resolve_load_order(modules)
    assert [m["name"] for m in ordered] == ["bad"]
    assert any("depends must be a list" in w for w in loader.warnings)


def test_load_all_initializes_modules(tmp_path):
    d = tmp_path / "cmdmod"
    d.mkdir()
    (d / "manifest.toml").write_text('name = "cmdmod"\nversion = "1.0.0"\n')
    (d / "init.lua").write_text(
        'function init(ctx)\n  ctx.services.get("commands"):register("/hello", function(args) return "hi" end)\nend\n'
    )
    loader = ModuleLoader(tmp_path, organism=None, modules_config={"enabled": ["cmdmod"]})
    loader.load_all()
    result = loader.registry.get("commands").dispatch("/hello", [])
    assert result == "hi"


def test_load_all_empty_whitelist_loads_none(tmp_path):
    d = tmp_path / "cmdmod"
    d.mkdir()
    (d / "manifest.toml").write_text('name = "cmdmod"\nversion = "1.0.0"\n')
    (d / "init.lua").write_text("function init(ctx) end\n")
    loader = ModuleLoader(tmp_path, organism=None, modules_config={"enabled": []})
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
    lua_code = "function init(ctx)\n" + "\n".join("  " + c for c in checks) + "\nend\n"
    (d / "init.lua").write_text(lua_code)
    loader = ModuleLoader(tmp_path, organism=None, modules_config={"enabled": ["sandbox"]})
    loader.load_all()
    assert "sandbox" in loader.modules
    assert not any("sandbox" in w for w in loader.warnings)


def test_lua_sandbox_blocks_python_global(tmp_path):
    d = tmp_path / "pwn"
    d.mkdir()
    (d / "manifest.toml").write_text('name = "pwn"\nversion = "1.0.0"\n')
    (d / "init.lua").write_text('function init(ctx)\n  if python ~= nil then error("python global leaked") end\nend\n')
    loader = ModuleLoader(tmp_path, organism=None, modules_config={"enabled": ["pwn"]})
    loader.load_all()
    assert "pwn" in loader.modules
    assert not any("python" in w for w in loader.warnings)


def test_lua_sandbox_blocks_python_rce_in_modules(tmp_path):
    d = tmp_path / "rce"
    d.mkdir()
    (d / "manifest.toml").write_text('name = "rce"\nversion = "1.0.0"\n')
    (d / "init.lua").write_text("function init(ctx)\n  python.none.__subclasses__()\nend\n")
    loader = ModuleLoader(tmp_path, organism=None, modules_config={"enabled": ["rce"]})
    loader.load_all()
    assert "rce" not in loader.modules
    assert any("python" in w.lower() or "nil" in w.lower() for w in loader.warnings)


def test_lua_sandbox_blocks_dunder_escape_via_services(tmp_path):
    """Regression: ctx.services is a live Python object; without dunder
    blocking, obj.__class__.__base__.__subclasses__() escapes to Python."""
    d = tmp_path / "dunder"
    d.mkdir()
    (d / "manifest.toml").write_text('name = "dunder"\nversion = "1.0.0"\n')
    (d / "init.lua").write_text(
        "function init(ctx)\n"
        "  local ok, res = pcall(function() return ctx.services.__class__ end)\n"
        "  if ok and res ~= nil then error('ESCAPED: __class__ reachable') end\n"
        "  local ok2, res2 = pcall(function() return ctx.log.__globals__ end)\n"
        "  if ok2 and res2 ~= nil then error('ESCAPED: __globals__ reachable') end\n"
        "end\n"
    )
    loader = ModuleLoader(tmp_path, organism=None, modules_config={"enabled": ["dunder"]})
    loader.load_all()
    assert "dunder" in loader.modules
    assert not any("ESCAPED" in w for w in loader.warnings)


def test_lua_sandbox_to_py_converts_nested_tables():
    from replicanta import lua_sandbox

    lua = lua_sandbox.build_runtime()
    tbl = lua.execute("return {name = 'x', nested = {a = 1}, list = {10, 20}}")
    assert lua_sandbox.to_py(tbl) == {"name": "x", "nested": {"a": 1}, "list": [10, 20]}
    assert lua_sandbox.to_py({"already": "py"}) == {"already": "py"}


def test_module_context_exposes_events(tmp_path):
    mod = tmp_path / "mods" / "evt"
    mod.mkdir(parents=True)
    (mod / "manifest.toml").write_text('name = "evt"\n')
    (mod / "init.lua").write_text(
        "function init(ctx)\n"
        '  ctx.events:declare("ping")\n'
        '  ctx.events:on("ping", function(text) ctx.log("pong:" .. tostring(text)) end)\n'
        "end\n"
    )
    logs = []
    loader = ModuleLoader(tmp_path / "mods", emit=logs.append, modules_config={"enabled": ["evt"]})
    loader.load_all()
    loader.registry.get("hooks").emit("ping", "hello")
    assert "pong:hello" in logs
    assert "ping" in loader.registry.get("hooks").known()


# -- organism facade (sandbox surface) ------------------------------------------


def _write_probe_module(mods_dir, body):
    mod = mods_dir / "probe"
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "manifest.toml").write_text('name = "probe"\n')
    (mod / "init.lua").write_text("function init(ctx)\n" + body + "\nend\n")


def test_organism_service_is_a_facade_not_the_live_organism(tmp_path):
    """Regression: the registry used to hand sandboxed Lua the live Organism
    whose public Path attributes let a module walk the host filesystem.
    Only the narrow facade may be reachable, and every escape hatch must
    fail (init records what it could reach; the harness asserts nothing did)."""
    from types import SimpleNamespace

    class _Store:
        def __init__(self):
            self.dir_path = tmp_path

    mods = tmp_path / "mods"
    _write_probe_module(
        mods,
        "  local org = ctx.services.get('organism')\n"
        "  if org == nil then error('no organism service') end\n"
        "  -- string-only data: fine\n"
        "  local nm = org:name()\n"
        "  if type(nm) ~= 'string' then error('name() not a string') end\n"
        "  -- every filesystem-relevant attribute must be unreachable\n"
        "  local tries = {\n"
        "    function() return org.dir_path end,\n"
        "    function() return org.store end,\n"
        "    function() return org.dir_path.parents end,\n"
        "    function() return org:name().parents end,\n"
        "    function() return org['dir_path'] end,\n"
        "    function() return org.flush end,\n"
        "    function() return org.close end,\n"
        "  }\n"
        "  for i, t in ipairs(tries) do\n"
        "    local ok, v = pcall(t)\n"
        "    if ok and v ~= nil then error('escape ' .. i .. ' reached ' .. tostring(v)) end\n"
        "  end\n",
    )
    loader = ModuleLoader(
        mods,
        organism=SimpleNamespace(dir_path=tmp_path, store=_Store()),
        modules_config={"enabled": ["probe"]},
    )
    loader.load_all()
    assert "probe" in loader.modules
    assert not any("escape" in w for w in loader.warnings)


def test_organism_facade_entity_actuation_flag(tmp_path):
    """The facade exposes the persisted entity_actuation toggle; organisms
    without the attribute (older saves, doubles) default to enabled."""
    from types import SimpleNamespace

    class _Store:
        def __init__(self):
            self.dir_path = tmp_path

    def _load(org):
        mods = tmp_path / "mods-act"
        if mods.exists():
            import shutil

            shutil.rmtree(mods)
        _write_probe_module(
            mods,
            "  local org = ctx.services.get('organism')\n  ctx.log('actuation=' .. tostring(org:entity_actuation()))\n",
        )
        logs = []
        loader = ModuleLoader(mods, organism=org, modules_config={"enabled": ["probe"]}, emit=logs.append)
        loader.load_all()
        return logs

    enabled = SimpleNamespace(dir_path=tmp_path, store=_Store(), entity_actuation=True)
    disabled = SimpleNamespace(dir_path=tmp_path, store=_Store(), entity_actuation=False)
    legacy = SimpleNamespace(dir_path=tmp_path, store=_Store())
    assert "actuation=true" in _load(enabled)
    assert "actuation=false" in _load(disabled)
    assert "actuation=true" in _load(legacy)


def test_store_service_hides_the_live_store(tmp_path):
    """Regression: _StoreService exposed `.store` — the live BeliefStore with
    Path attributes and every method. Only observe/remember may be reachable."""
    from types import SimpleNamespace

    class _Store:
        def __init__(self):
            self.dir_path = tmp_path
            self.touched = []

        def observe(self, belief, conf):
            self.touched.append(("observe", belief, conf))

        def remember(self, kind, text):
            self.touched.append(("remember", kind, text))

    store = _Store()
    mods = tmp_path / "mods-store"
    _write_probe_module(
        mods,
        "  local svc = ctx.services.get('store')\n"
        "  if svc == nil then error('no store service') end\n"
        "  svc:remember('flybrain', 'ran digits')\n"
        "  local ok, v = pcall(function() return svc.store end)\n"
        "  if ok and v ~= nil then error('live store reachable') end\n"
        "  local ok2, v2 = pcall(function() return svc.store.dir_path end)\n"
        "  if ok2 and v2 ~= nil then error('store dir_path reachable') end\n",
    )
    loader = ModuleLoader(
        mods,
        organism=SimpleNamespace(dir_path=tmp_path, store=store),
        modules_config={"enabled": ["probe"]},
    )
    loader.load_all()
    assert "probe" in loader.modules
    assert ("remember", "flybrain", "ran digits") in store.touched
    assert not any("reachable" in w for w in loader.warnings)


# -- ctx.after / ctx.every / ctx.kv / ctx.call -----------------------------------


def _ctx_probe(tmp_path, body, organism=None):
    mods = tmp_path / "mods"
    _write_probe_module(mods, body)
    logs = []
    loader = ModuleLoader(mods, organism=organism, modules_config={"enabled": ["probe"]}, emit=logs.append)
    loader.load_all()
    return loader, logs


def test_ctx_after_and_every_fire_and_cancel(tmp_path):
    import time

    _loader, logs = _ctx_probe(
        tmp_path,
        "  ctx.after(50, function() ctx.log('after-fired') end)\n"
        "  local n = 0\n"
        "  local h = ctx.every(40, function()\n"
        "    n = n + 1\n"
        "    ctx.log('every-' .. n)\n"
        "    if n >= 100 then h:cancel() end\n"
        "  end)\n"
        "  ctx.log('handle-' .. tostring(h ~= nil))\n"
        "  _G.probe_handle = h\n",
    )
    assert "handle-true" in logs
    deadline = time.monotonic() + 5.0
    while "after-fired" not in logs and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "after-fired" in logs
    deadline = time.monotonic() + 5.0
    while not any(line.startswith("every-") for line in logs) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert any(line.startswith("every-") for line in logs)
    # a raising handler is contained: one error line, timers keep living
    loader2, logs2 = _ctx_probe(
        tmp_path.parent / ("t2-" + tmp_path.name),
        "  ctx.after(20, function() error('boom') end)\n  ctx.after(60, function() ctx.log('survivor') end)\n",
    )
    deadline = time.monotonic() + 5.0
    while "survivor" not in logs2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "survivor" in logs2
    assert any("boom" in line for line in logs2)
    loader2.registry.shutdown()


def test_ctx_timer_handle_cancel_stops_every(tmp_path):
    import time

    loader, logs = _ctx_probe(
        tmp_path,
        "  local h = ctx.every(30, function() ctx.log('tick') end)\n  ctx.after(120, function() h:cancel() end)\n",
    )
    time.sleep(0.6)
    ticks = [line for line in logs if line == "tick"]
    assert 1 <= len(ticks) <= 8  # fired for a while, then cancelled
    loader.registry.shutdown()


def test_ctx_kv_persists_across_reloads(tmp_path):
    from types import SimpleNamespace

    class _Store:
        def __init__(self):
            self.dir_path = tmp_path

    organism = SimpleNamespace(dir_path=tmp_path, store=_Store())
    mods = tmp_path / "mods-kv"
    _write_probe_module(
        mods,
        "  local seen = ctx.kv:get('count', 0)\n"
        "  ctx.kv:set('count', seen + 1)\n"
        "  ctx.kv:set('nested', {a = 1, list = {1, 2}})\n"
        "  ctx.log('seen-' .. tostring(seen))\n",
    )
    loader = ModuleLoader(mods, organism=organism, modules_config={"enabled": ["probe"]})
    loader.load_all()
    logs = []
    loader.emit = logs.append
    # reload into a fresh loader: the FILE is the source of truth
    loader2 = ModuleLoader(mods, organism=organism, modules_config={"enabled": ["probe"]}, emit=logs.append)
    loader2.load_all()
    assert "seen-1" in logs
    assert (tmp_path / "kv" / "probe.json").is_file()

    def read_kv():
        import json

        return json.loads((tmp_path / "kv" / "probe.json").read_text())

    assert read_kv()["count"] == 2
    assert read_kv()["nested"] == {"a": 1, "list": [1, 2]}


def test_ctx_call_routes_and_reports_errors(tmp_path):
    _loader, logs = _ctx_probe(
        tmp_path,
        "  local r, err = ctx.call('commands.has(\"/nope\")')\n"
        "  ctx.log('has=' .. tostring(r) .. ' err=' .. tostring(err))\n"
        "  local r2, err2 = ctx.call('commands.nope(1)')\n"
        "  ctx.log('unknown=' .. tostring(r2) .. ' err2=' .. tostring(err2))\n"
        "  local r3, err3 = ctx.call('nosvc.run(1)')\n"
        "  ctx.log('nosvc=' .. tostring(r3) .. ' err3=' .. tostring(err3))\n"
        "  local r4, err4 = ctx.call('prose line, not a call')\n"
        "  ctx.log('notcall=' .. tostring(r4) .. ' err4=' .. tostring(err4))\n",
    )
    assert "has=false err=nil" in logs or any("has=false" in line and "err=nil" in line for line in logs)
    assert any("unknown=nil" in line and "unknown method" in line for line in logs)
    assert any("nosvc=nil" in line and "unknown service" in line for line in logs)
    assert any("notcall=nil" in line and "not a service call" in line for line in logs)


def test_ctx_call_parse_coercion(tmp_path):
    from replicanta.modules import CallService

    svc = CallService(registry=None)  # parse only; no registry needed
    assert svc.parse('doom.command("shoot")') == ("doom", "command", "shoot")
    assert svc.parse("doom.command(shoot)") == ("doom", "command", "shoot")
    assert svc.parse('hand.move("wave", 3)') == ("hand", "move", "wave", 3)
    assert svc.parse('hand.move("wave", 3.5)') == ("hand", "move", "wave", 3.5)
    assert svc.parse("svc.flag(true, nil)") == ("svc", "flag", True, None)
    assert svc.parse('brain.run "digits"') == ("brain", "run", "digits")
    assert svc.parse("just prose") is None
    assert svc.parse("svc._private(1)") is None
    assert svc.parse("_svc.run(1)") is None


# -- registry shutdown ------------------------------------------------------------


def test_registry_shutdown_is_scoped_and_idempotent(tmp_path):
    """One registry's shutdown must reap only ITS modules' children (a
    per-organism reload must not kill another organism's games), must be
    idempotent, and must call services' shutdown()/stop()."""
    from replicanta import capbridges

    calls = []

    class _Svc:
        def shutdown(self):
            calls.append("shutdown")

    class _Other:
        def stop(self):
            calls.append("stop")

    reg_a = ServiceRegistry()
    reg_a.register("svc", _Svc())
    reg_a.register("other", _Other())
    reg_b = ServiceRegistry()

    bridge_a = capbridges.ProcessBridge(owner=reg_a)
    bridge_b = capbridges.ProcessBridge(owner=reg_b)
    import time
    from pathlib import Path

    stub = str(Path(__file__).parent / "fixtures" / "doom_ascii_stub.py")
    pid_a = bridge_a.spawn([stub, "--interval", "0.05"], {})
    pid_b = bridge_b.spawn([stub, "--interval", "0.05"], {})
    child_a = bridge_a._procs[pid_a]
    child_b = bridge_b._procs[pid_b]

    reg_a.shutdown()
    assert calls == ["shutdown", "stop"]  # preferred method per service
    assert not child_a.running()
    assert child_b.running()  # the other registry's child survives
    reg_a.shutdown()  # idempotent: no error, no double shutdown calls
    assert calls == ["shutdown", "stop"]

    reg_b.shutdown()
    deadline = time.monotonic() + 5.0
    while child_b.running() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not child_b.running()
