"""Headless tests for the module-management UX."""

import asyncio

from textual.widgets import Checkbox

from replicanta.modules import ModuleLoader
from replicanta.tui import ModulesScreen


def test_f9_binding_opens_modules_screen(headless_app):
    app = headless_app
    assert "f9" in app._bindings.key_to_bindings

    async def check():
        async with app.run_test() as pilot:
            await pilot.press("f9")
            await pilot.pause()
            assert isinstance(app.screen, ModulesScreen)

    asyncio.run(check())


def _make_modules_dir(tmp_path):
    modules_dir = tmp_path / "modules"
    for name in ("alpha", "beta"):
        d = modules_dir / name
        d.mkdir(parents=True)
        (d / "manifest.toml").write_text(f'name = "{name}"\nversion = "1.0.0"\n')
        (d / "init.lua").write_text("function init(ctx) end\n")
    return modules_dir


def test_modules_screen_toggles_enabled(headless_app, tmp_path):
    modules_dir = _make_modules_dir(tmp_path)
    loader = ModuleLoader(
        modules_dir,
        organism=None,
        modules_config={"enabled": ["alpha"]},
        root=tmp_path,
    )

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            app.push_screen(ModulesScreen(loader))
            await pilot.pause()
            screen = app.screen
            assert "alpha" in screen._enabled
            assert "beta" not in screen._enabled

            # Toggle beta on by clicking its Checkbox.
            beta_cb = screen.query_one("#mod-beta", Checkbox)
            beta_cb.value = True
            await pilot.pause()
            assert "beta" in screen._enabled
            assert "alpha" in screen._enabled

            # Toggle beta off again.
            beta_cb.value = False
            await pilot.pause()
            assert "beta" not in screen._enabled

    asyncio.run(check())


def test_modules_screen_save_persists_config(headless_app, tmp_path, monkeypatch):
    modules_dir = tmp_path / "modules"
    (modules_dir / "alpha").mkdir(parents=True)
    (modules_dir / "alpha" / "manifest.toml").write_text('name = "alpha"\nversion = "1.0.0"\n')
    (modules_dir / "alpha" / "init.lua").write_text("function init(ctx) end\n")

    loader = ModuleLoader(
        modules_dir,
        organism=None,
        modules_config={},
        root=tmp_path,
    )

    app = headless_app

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
        async with app.run_test() as pilot:
            screen = ModulesScreen(loader)
            screen._enabled = {"alpha"}
            app.push_screen(screen)
            await pilot.pause()
            screen.action_save()
            assert saved["called"]
            assert saved["root"] == tmp_path
            assert saved["config"]["modules"]["enabled"] == ["alpha"]
            assert reloaded["called"]

    asyncio.run(check())


def test_modules_screen_keyboard_toggle_and_save_propagates(headless_app, tmp_path):
    """End-to-end: open the screen with pilot keys, toggle beta, save, and verify
    the loader's modules_config picks up the new enabled set."""
    modules_dir = _make_modules_dir(tmp_path)
    loader = ModuleLoader(
        modules_dir,
        organism=None,
        modules_config={"enabled": ["alpha"]},
        root=tmp_path,
    )

    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            app.push_screen(ModulesScreen(loader))
            await pilot.pause()
            screen = app.screen
            assert "alpha" in screen._enabled
            assert "beta" not in screen._enabled

            # Focus beta's checkbox directly and press space to toggle.
            beta_cb = screen.query_one("#mod-beta", Checkbox)
            beta_cb.focus()
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            assert "beta" in screen._enabled

            # Save: this should write the config and reload modules on the loader.
            await pilot.press("s")
            await pilot.pause()
            assert loader.modules_config.get("enabled") == ["alpha", "beta"]

    asyncio.run(check())
