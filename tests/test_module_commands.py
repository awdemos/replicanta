import shutil
from pathlib import Path

import pytest

from replicanta.lua_sandbox import build_runtime
from replicanta.modules import CommandService, ModuleLoader

SEED = Path(__file__).parent.parent / "organism.scl"


def _write_echo_module(root):
    """Lay down an enabled Lua module that registers /echo with the
    CommandService, next to a replicanta.toml enabling it."""
    module = root / "modules" / "echocmd"
    module.mkdir(parents=True)
    (module / "manifest.toml").write_text('name = "echocmd"\nversion = "1.0.0"\n')
    (module / "init.lua").write_text(
        "function init(ctx)\n"
        '  ctx.services.get("commands"):register("/echo", function(args)\n'
        '    return "echo:" .. tostring(args[1]) .. ":" .. tostring(#args)\n'
        "  end)\n"
        "end\n"
    )
    (root / "replicanta.toml").write_text('[modules]\nenabled = ["echocmd"]\n')


def test_command_service_register_and_dispatch():
    cmds = CommandService()
    called = []
    cmds.register("/hi", lambda args: called.append(args))
    cmds.dispatch("/hi", ["a", "b"])
    assert called == [["a", "b"]]


def test_command_service_has_distinguishes_registered_names():
    cmds = CommandService()
    cmds.register("/hi", lambda args: None)
    assert cmds.has("/hi")
    assert not cmds.has("/nope")


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
    loader = ModuleLoader(tmp_path, organism=None, modules_config={"enabled": ["luacmd"]})
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


# -- the registry is the single dispatch seam for module commands ------------


def test_tui_dispatch_falls_through_to_module_command(monkeypatch, tmp_path):
    """A Lua-module-registered command dispatches through the TUI
    fallthrough even though _dispatch has no native branch for it."""
    from replicanta.organism import Organism
    from replicanta.tui import OrganismApp

    _write_echo_module(tmp_path)
    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    monkeypatch.setattr(app, "_probe_voice", lambda: None)
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
    monkeypatch.setattr(app, "_on_tick", lambda: None)
    logged = []
    monkeypatch.setattr(app, "_append_log", lambda text, style=None, stamp=False: logged.append(text))

    app._dispatch("/echo", ["/echo", "hi"])

    assert logged == ["echo:hi:1"]


def test_tui_dispatch_unknown_command_still_warns(monkeypatch, tmp_path):
    """Names no module registered keep the unknown-command behavior."""
    from replicanta.organism import Organism
    from replicanta.tui import OrganismApp

    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    monkeypatch.setattr(app, "_probe_voice", lambda: None)
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
    monkeypatch.setattr(app, "_on_tick", lambda: None)
    logged = []
    monkeypatch.setattr(app, "_append_log", lambda text, style=None, stamp=False: logged.append(text))

    app._dispatch("/nope", ["/nope"])

    assert logged == ["unknown: /nope (try /help)"]


def test_web_command_dispatches_module_registered_command(tmp_path):
    """The same Lua-registered command works through Glasshouse.command's
    fallthrough, so both UIs share the module dispatch seam."""
    from replicanta import nursery
    from replicanta.organism import Organism
    from replicanta.web import Glasshouse

    shutil.copy(SEED, tmp_path / "organism.scl")
    _write_echo_module(tmp_path)
    org_dir = nursery.create(tmp_path, "default", tmp_path / "organism.scl")
    nursery.set_current(tmp_path, "default")
    org = Organism(org_dir)
    org.load()
    app = Glasshouse(tmp_path, org, respond=lambda org, text: "heard")
    try:
        result = app.command("/echo hi")
        assert result["messages"] == ["echo:hi:1"]
    finally:
        org.mind.close()
