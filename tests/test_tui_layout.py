"""Layout regression tests for the workspace chrome."""

import asyncio
import sys
from pathlib import Path

from textual.containers import VerticalScroll
from textual.widgets import ListView, Static

sys.path.insert(0, str(Path(__file__).parent.parent))

from organism import Organism
from tui import OrganismApp


def _headless_app(monkeypatch, tmp_path):
    import nursery as nursery_mod
    seed = tmp_path / "organism.scl"
    seed.write_text('type bel(x: String, a: String, v: String)\n')
    nursery_mod.create(tmp_path, "default", seed)
    org = Organism(nursery_mod.organism_dir(tmp_path, "default"))
    org.load()
    app = OrganismApp(org, root=tmp_path)
    monkeypatch.setattr(app, "_probe_voice", lambda: None)
    monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
    monkeypatch.setattr(app, "_on_tick", lambda: None)
    return app


def _renderable_text(widget):
    """Read a Static widget's current content as a plain string.

    Textual 8 stores Static content in the name-mangled private
    attribute `_Static__content`.
    """
    return str(getattr(widget, "_Static__content", ""))


def test_top_bar_shows_organism_name(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            app.refresh_top_bar()
            text = _renderable_text(app.query_one("#topbar", Static))
            assert "Replicanta" in text
            name = Path(app.org.dir_path).name
            assert name in text

    asyncio.run(check())


def test_sidebar_lists_organisms_and_highlights_current(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    (app.root / "organisms" / "fern").mkdir(parents=True)

    async def check():
        async with app.run_test():
            app._refresh_sidebar()
            await asyncio.sleep(0.05)
            lv = app.query_one("#sidebar-list", ListView)
            current = Path(app.org.dir_path).name
            labels = [str(_renderable_text(item.children[0])) for item in lv.children]
            assert any(current in label for label in labels)
            assert any("fern" in label for label in labels)

    asyncio.run(check())


def _make_fern(app):
    """Birth a second organism 'fern' in the test nursery."""
    fern_dir = app.root / "organisms" / "fern"
    fern_dir.mkdir(parents=True)
    seed = app.root / "organism.scl"
    if seed.exists():
        (fern_dir / "organism.scl").write_text(seed.read_text())


def test_sidebar_selection_opens_action_menu(monkeypatch, tmp_path):
    """Left-click / Enter on a sidebar organism opens its dropdown menu
    instead of swapping immediately."""
    from tui import OrganismMenuScreen
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)

    async def check():
        async with app.run_test():
            app._refresh_sidebar()
            await asyncio.sleep(0.05)
            lv = app.query_one("#sidebar-list", ListView)
            fern_item = next(
                item for item in lv.children
                if "fern" in str(_renderable_text(item.children[0])))
            event = type("Selected", (), {"item": fern_item})()
            app.on_list_view_selected(event)
            await asyncio.sleep(0.05)
            assert isinstance(app.screen, OrganismMenuScreen)
            assert app.org.dir_path.name == "default"  # not swapped yet

    asyncio.run(check())


def test_sidebar_menu_swap_choice_swaps_organism(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)

    async def check():
        async with app.run_test():
            app._refresh_sidebar()
            await asyncio.sleep(0.05)
            app._open_org_menu("fern")
            await asyncio.sleep(0.05)
            app.screen.dismiss(("swap", "fern"))
            await asyncio.sleep(0.05)
            assert app.org.dir_path.name == "fern"

    asyncio.run(check())


def test_rename_sleeping_organism_refreshes_sidebar(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)

    async def check():
        async with app.run_test():
            app._rename_org("fern", "willow")
            await asyncio.sleep(0.05)
            assert (app.root / "organisms" / "willow").is_dir()
            assert app.org.dir_path.name == "default"
            lv = app.query_one("#sidebar-list", ListView)
            labels = [str(_renderable_text(item.children[0]))
                      for item in lv.children]
            assert any("willow" in label for label in labels)
            assert not any("fern" in label for label in labels)

    asyncio.run(check())


def test_rename_awake_organism_swaps_to_new_path(monkeypatch, tmp_path):
    """Renaming the awake organism moves its directory and keeps the app
    living with it — the old path must not be resurrected by a flush."""
    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            app._rename_org("default", "willow")
            await asyncio.sleep(0.05)
            assert app.org.dir_path.name == "willow"
            assert not (app.root / "organisms" / "default").exists()
            assert (app.root / "organisms" / "willow").is_dir()
            import nursery as nursery_mod
            assert nursery_mod.current(app.root) == "willow"

    asyncio.run(check())


def test_menu_for_awake_organism_has_no_swap_option(monkeypatch, tmp_path):
    from textual.widgets import OptionList
    from tui import OrganismMenuScreen
    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            app._open_org_menu("default")
            await asyncio.sleep(0.05)
            assert isinstance(app.screen, OrganismMenuScreen)
            menu = app.screen.query_one(OptionList)
            ids = [option.id for option in menu.options]
            assert ids == ["rename", "cancel"]

    asyncio.run(check())


def test_bottom_bar_shows_counts_and_keys(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            app.refresh_status()
            text = _renderable_text(app.query_one("#bottombar", Static))
            assert "beliefs" in text
            assert "rules" in text
            assert "ctrl+q quit" in text

    asyncio.run(check())


def test_mind_memory_inner_are_scrollable(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            for pane in ("mind-pane", "memory-pane", "inner-pane"):
                tab = app.query_one(f"#{pane}")
                scroll = tab.query_one(VerticalScroll)
                assert scroll is not None

    asyncio.run(check())


def test_chat_input_stays_below_main_area(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            main = app.query_one("#main")
            chat = app.query_one("#chat")
            bottom = app.query_one("#bottombar")
            assert main.styles.height.value == 1.0  # 1fr
            assert chat.styles.height.value == 3
            assert bottom.styles.height.value == 1

    asyncio.run(check())
