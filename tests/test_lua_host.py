"""LuaHost: one runtime hosts scripts and modules; dispatch reaches both."""

from pathlib import Path

from replicanta.lua_host import LuaHost


def _write_module(mods: Path, name: str, init_lua: str):
    mod = mods / name
    mod.mkdir(parents=True)
    (mod / "manifest.toml").write_text(f'name = "{name}"\n')
    (mod / "init.lua").write_text(init_lua)


def test_host_loads_modules_and_scripts_on_one_runtime(tmp_path):
    mods = tmp_path / "mods"
    _write_module(
        mods,
        "pinger",
        "function init(ctx)\n"
        '  ctx.events:declare("ping")\n'
        '  ctx.events:on("ping", function(text) ctx.log("mod:" .. tostring(text)) end)\n'
        "end\n",
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "s.lua").write_text('function on_ping(ctx) ctx.log("script:" .. tostring(ctx.text)) end\n')
    logs = []
    host = LuaHost(scripts_dir=scripts, modules_dir=mods, emit=logs.append)
    host.load_modules(modules_config={"enabled": ["pinger"]})
    host.reload_scripts()
    host.fire("ping", text="hello")
    assert "mod:hello" in logs
    assert "script:hello" in logs


def test_host_fire_guards_broken_handlers(tmp_path):
    mods = tmp_path / "mods"
    _write_module(
        mods,
        "breaker",
        'function init(ctx)\n  ctx.events:on("cycle", function(_text) error("boom") end)\nend\n',
    )
    logs = []
    host = LuaHost(scripts_dir=tmp_path / "scripts", modules_dir=mods, emit=logs.append)
    host.load_modules(modules_config={"enabled": ["breaker"]})
    host.fire("cycle", text="wake")  # must not raise
    assert any("boom" in line for line in logs)


def test_host_run_executes_script_main(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "tool.lua").write_text('function main(ctx) ctx.log("ran:" .. ctx.event) end\n')
    logs = []
    host = LuaHost(scripts_dir=scripts, modules_dir=tmp_path / "mods", emit=logs.append)
    host.reload_scripts()
    assert host.run("tool.lua", org=None) == "lua: ran tool.lua"
    assert any("ran:lua" in line for line in logs)


def test_host_passes_its_lock_to_the_arm_service(tmp_path):
    """The volition thread must serialize with host dispatch through the same lock."""
    host = LuaHost(scripts_dir=tmp_path / "scripts", modules_dir=tmp_path / "mods", emit=lambda _m: None)
    host.load_modules(modules_config={"enabled": []})
    arm = host.registry.get("arm")
    assert arm is not None
    assert arm._lua_lock is host.lock
