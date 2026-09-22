"""Text selection copies to the clipboard on release.

With terminal mouse reporting on (needed for clicks, submenu boxes, and
sidebar drags), the terminal's own selection is unreachable, so the app
copies Textual's selection itself via screen.copy_text (OSC52) when the
drag ends.
"""

import asyncio


def test_drag_release_copies_selection(nursery_app, monkeypatch):
    app = nursery_app
    copied = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)

    async def check():
        async with app.run_test():
            monkeypatch.setattr(app.screen, "get_selected_text", lambda: "selected words")
            app.on_text_selected(None)
            assert copied == ["selected words"]

    asyncio.run(check())


def test_release_without_selection_copies_nothing(nursery_app, monkeypatch):
    app = nursery_app
    copied = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)

    async def check():
        async with app.run_test():
            monkeypatch.setattr(app.screen, "get_selected_text", lambda: None)
            app.on_text_selected(None)  # SkipAction must be swallowed
            assert copied == []

    asyncio.run(check())
