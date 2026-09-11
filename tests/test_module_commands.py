import pytest

from replicanta.lua_sandbox import build_runtime
from replicanta.modules import CommandService, ModuleLoader


def test_command_service_register_and_dispatch():
    cmds = CommandService()
    called = []
    cmds.register("/hi", lambda args: called.append(args))
    cmds.dispatch("/hi", ["a", "b"])
    assert called == [["a", "b"]]


def test_command_service_unknown_returns_none():
    cmds = CommandService()
    assert cmds.dispatch("/unknown", []) is None


def test_dispatch_converts_args_to_table_for_lua_handler(tmp_path):
    """A Lua handler receives args as a table built in the loader's own
    Lua runtime; the returned string comes back to Python."""
    d = tmp_path / "luacmd"
    d.mkdir()
    (d / "manifest.toml").write_text('name = "luacmd"\nversion = "1.0.0"\n')
    (d / "init.lua").write_text(
        "function init(ctx)\n"
        '  ctx.services.get("commands"):register("/echo", function(args)\n'
        '    return tostring(args[1]) .. "," .. tostring(args[2]) .. "," .. tostring(#args)\n'
        "  end)\n"
        "end\n"
    )
    loader = ModuleLoader(tmp_path, organism=None, config={"modules": {"enabled": ["luacmd"]}})
    loader.load_all()
    result = loader.registry.get("commands").dispatch("/echo", ["a", "b"])
    assert result == "a,b,2"


def test_dispatch_lua_handler_without_loader_fails_clearly():
    """Without a loader there is no runtime to build the args table in;
    the old fallback built a table in a fresh runtime the handler rejects."""
    cmds = CommandService()
    lua = build_runtime()
    cmds.register("/hi", lua.eval("function(args) return args[1] end"))
    with pytest.raises(RuntimeError, match="loader"):
        cmds.dispatch("/hi", ["a"])
