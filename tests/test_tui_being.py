"""The being overlay (F3): the mind/memory/inner/cells dashboards as one
full-screen scroll, opened by F3 and dismissed with escape."""

import asyncio

from textual.containers import VerticalScroll
from textual.widgets import Label, Static

from conftest import renderable_text
from replicanta.tui import BeingScreen


def test_f3_pushes_being_screen_with_all_sections(headless_app):
    """F3 must open the overlay carrying the MIND, MEMORY, INNER, and CELLS
    sections, each with live content; escape must pop it."""
    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.pause()
            assert type(app.screen) is BeingScreen
            assert app.screen.query_one("#being-scroll", VerticalScroll)
            headers = [renderable_text(label) for label in app.screen.query(Label)]
            joined = "\n".join(headers)
            for section in ("MIND", "MEMORY", "INNER", "CELLS"):
                assert section in joined, f"{section} header missing: {joined!r}"
            # every section renders its (empty-state) content
            mind = renderable_text(app.screen.query_one("#mind", Static))
            memory = renderable_text(app.screen.query_one("#memory", Static))
            inner = renderable_text(app.screen.query_one("#inner", Static))
            cells = renderable_text(app.screen.query_one("#cells", Static))
            assert "No beliefs yet" in mind
            assert "episodes" in memory  # the birth episode is already there
            assert "mental state" in inner
            assert "neural memory" in cells
            await pilot.press("escape")
            await pilot.pause()
            assert type(app.screen) is not BeingScreen

    asyncio.run(check())


def test_being_screen_reflects_organism_state(headless_app):
    """The overlay renders live store content, not just empty states."""
    app = headless_app
    app.org.store.add(("user", "name", "sam"), 0.8)
    app.org.store.remember("learned", "your name is sam")

    async def check():
        async with app.run_test() as pilot:
            app.action_being()
            await pilot.pause()
            assert type(app.screen) is BeingScreen
            mind = renderable_text(app.screen.query_one("#mind", Static))
            memory = renderable_text(app.screen.query_one("#memory", Static))
            assert "user:name=sam" in mind
            assert "your name is sam" in memory
            # a tick refresh re-renders the sections against the live organism
            app.screen.refresh_sections(app.org)
            await pilot.pause()
            assert "user:name=sam" in renderable_text(app.screen.query_one("#mind", Static))

    asyncio.run(check())


def test_being_screen_sections_refresh_from_tick(headless_app):
    """While the overlay is up, the per-tick view refresh updates it."""
    app = headless_app

    async def check():
        async with app.run_test() as pilot:
            app.action_being()
            await pilot.pause()
            app.org.store.add(("user", "name", "sam"), 0.8)
            app._refresh_views()  # what _on_tick calls every second
            await pilot.pause()
            mind = renderable_text(app.screen.query_one("#mind", Static))
            assert "user:name=sam" in mind

    asyncio.run(check())
