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


def test_sidebar_selection_swaps_organism(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    fern_dir = app.root / "organisms" / "fern"
    fern_dir.mkdir(parents=True)
    seed = app.root / "organism.scl"
    if seed.exists():
        (fern_dir / "organism.scl").write_text(seed.read_text())

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
            assert app.org.dir_path.name == "fern"

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
