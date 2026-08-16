"""Headless tests for the module-management UX."""

import asyncio

from textual.widgets import ListItem

from replicanta.modules import ModuleLoader
from replicanta.organism import Organism
from replicanta.tui import ModulesScreen, OrganismApp


def _headless_app(monkeypatch, tmp_path):
    org = Organism(tmp_path)
    org.load()
    app = OrganismApp(org)
    monkeypatch.setattr(app, "_probe_voice", lambda: None)
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
    monkeypatch.setattr(app, "_on_tick", lambda: None)
    return app


def test_f9_binding_opens_modules_screen(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    assert "f9" in app._bindings.key_to_bindings

    async def check():
        async with app.run_test() as _pilot:
            await _pilot.press("f9")
            await asyncio.sleep(0.1)
            assert isinstance(app.screen, ModulesScreen)

    asyncio.run(check())


def _make_modules_dir(tmp_path):
    modules_dir = tmp_path / "modules"
    for name in ("alpha", "beta"):
        d = modules_dir / name
        d.mkdir(parents=True)
        (d / "manifest.toml").write_text(
            f'name = "{name}"\nversion = "1.0.0"\n'
        )
        (d / "init.lua").write_text("function init(ctx) end\n")
    return modules_dir


def test_modules_screen_toggles_enabled(monkeypatch, tmp_path):
    modules_dir = _make_modules_dir(tmp_path)
    loader = ModuleLoader(
        modules_dir,
        organism=None,
        config={"modules": {"enabled": ["alpha"]}},
        root=tmp_path,
    )

    app = OrganismApp(Organism(tmp_path))
    monkeypatch.setattr(app, "_probe_voice", lambda: None)
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
    monkeypatch.setattr(app, "_on_tick", lambda: None)

    async def check():
        async with app.run_test() as _pilot:
            app.push_screen(ModulesScreen(loader))
            await asyncio.sleep(0.1)
            screen = app.screen
            assert "alpha" in screen._enabled
            assert "beta" not in screen._enabled

            # Get the actual ListItem for beta and toggle it.
            beta_item = screen.query_one("#mod-beta", ListItem)

            class FakeEvent:
                item = beta_item

            screen.on_list_view_selected(FakeEvent())
            assert "beta" in screen._enabled
            assert "alpha" in screen._enabled

            # Select beta again to toggle it off.
            screen.on_list_view_selected(FakeEvent())
            assert "beta" not in screen._enabled

    asyncio.run(check())


def test_modules_screen_save_persists_config(tmp_path, monkeypatch):
    modules_dir = tmp_path / "modules"
    (modules_dir / "alpha").mkdir(parents=True)
    (modules_dir / "alpha" / "manifest.toml").write_text(
        'name = "alpha"\nversion = "1.0.0"\n'
    )
    (modules_dir / "alpha" / "init.lua").write_text("function init(ctx) end\n")

    loader = ModuleLoader(
        modules_dir,
        organism=None,
        config={"modules": {}},
        root=tmp_path,
    )

    app = OrganismApp(Organism(tmp_path))
    monkeypatch.setattr(app, "_probe_voice", lambda: None)
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
    monkeypatch.setattr(app, "_on_tick", lambda: None)

    saved = {"called": False, "root": None, "config": None}

    def fake_save(root, config):
        saved["called"] = True
        saved["root"] = root
        saved["config"] = config

    from replicanta import config as project_config

    monkeypatch.setattr(project_config, "save_config", fake_save)
    reloaded = {"called": False}
    monkeypatch.setattr(loader, "load_all", lambda: reloaded.update(called=True))

    async def check():
        async with app.run_test() as _pilot:
            screen = ModulesScreen(loader)
            screen._enabled = {"alpha"}
            app.push_screen(screen)
            await asyncio.sleep(0.1)
            screen.action_save()
            assert saved["called"]
            assert saved["root"] == tmp_path
            assert saved["config"]["modules"]["enabled"] == ["alpha"]
            assert reloaded["called"]

    asyncio.run(check())
