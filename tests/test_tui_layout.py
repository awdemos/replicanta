"""Layout regression tests for the workspace chrome."""

import asyncio
from pathlib import Path

from textual.widgets import ListView, Static

from conftest import renderable_text, wait_until


def test_top_bar_shows_organism_name(nursery_app):
    app = nursery_app

    async def check():
        async with app.run_test():
            app.refresh_top_bar()
            text = renderable_text(app.query_one("#topbar", Static), width=120)
            assert "REPLICANTA" in text  # wordmark is upper-case in the new branding
            name = Path(app.org.dir_path).name
            assert name in text
            assert "a/c/i" in text  # mental-state readout in the center zone
            assert "UTC" in text  # clock in the right zone

    asyncio.run(check())


def test_top_bar_shows_module_badges(nursery_app, monkeypatch):
    """The capability glyphs (fly brain etc.) must be visible on the main
    screen's top bar — the sidebar (their only previous home) is hidden by
    default, so the badges were effectively invisible."""
    app = nursery_app
    monkeypatch.setattr(app, "_module_badges", lambda: " 🪰 💀")

    async def check():
        async with app.run_test():
            app.refresh_top_bar()
            text = renderable_text(app.query_one("#topbar", Static), width=120)
            assert "🪰" in text and "💀" in text

    asyncio.run(check())


def test_sidebar_badges_come_from_each_organisms_own_config(nursery_app):
    """Regression: every sidebar row showed the CURRENT organism's badges.
    A row's glyphs must come from that organism's own module config."""
    app = nursery_app
    (app.root / "organisms" / "fern").mkdir(parents=True)
    (app.root / "organisms" / "fern" / "replicanta.toml").write_text('[modules]\nenabled = ["base", "fly-brain"]\n')

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            lv = app.query_one("#sidebar-list", ListView)
            labels = [renderable_text(item.children[0]) for item in lv.children]
            fern = next(label for label in labels if "fern" in label)
            assert "🪰" in fern, f"fern row lacks its fly-brain badge: {fern!r}"
            current = Path(app.org.dir_path).name
            mine = next(label for label in labels if current in label)
            assert "🪰" not in mine, f"current row shows another organism's badge: {mine!r}"

    asyncio.run(check())


def test_sidebar_lists_organisms_and_highlights_current(nursery_app):
    app = nursery_app
    (app.root / "organisms" / "fern").mkdir(parents=True)

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            lv = app.query_one("#sidebar-list", ListView)
            current = Path(app.org.dir_path).name
            labels = [renderable_text(item.children[0]) for item in lv.children]
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


def test_sidebar_selection_swaps_immediately(nursery_app):
    """Left-click / Enter on a sidebar organism swaps to it directly — no
    confirmation menu. (Rename / move-to-group live on right-click.)"""
    from textual.screen import ModalScreen

    app = nursery_app
    _make_fern(app)

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            lv = app.query_one("#sidebar-list", ListView)
            fern_item = next(item for item in lv.children if "fern" in renderable_text(item.children[0]))
            index = list(lv.children).index(fern_item)
            app.on_list_view_selected(ListView.Selected(lv, fern_item, index))
            await pilot.pause()
            assert app.org.dir_path.name == "fern"
            assert not isinstance(app.screen, ModalScreen)  # no menu opened

    asyncio.run(check())


def test_sidebar_selection_of_current_organism_is_noop(nursery_app):
    app = nursery_app
    _make_fern(app)

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            lv = app.query_one("#sidebar-list", ListView)
            current_item = next(item for item in lv.children if "default" in renderable_text(item.children[0]))
            index = list(lv.children).index(current_item)
            app.on_list_view_selected(ListView.Selected(lv, current_item, index))
            await pilot.pause()
            assert app.org.dir_path.name == "default"

    asyncio.run(check())


def test_sidebar_menu_swap_choice_swaps_organism(nursery_app):
    app = nursery_app
    _make_fern(app)

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            app._open_org_menu("fern")
            await pilot.pause()
            app.screen.dismiss(("swap", "fern"))
            await pilot.pause()
            assert app.org.dir_path.name == "fern"

    asyncio.run(check())


def test_rename_sleeping_organism_refreshes_sidebar(nursery_app):
    app = nursery_app
    _make_fern(app)

    async def check():
        async with app.run_test() as pilot:
            app._rename_org("fern", "willow")
            await pilot.pause()
            assert (app.root / "organisms" / "willow").is_dir()
            assert app.org.dir_path.name == "default"
            lv = app.query_one("#sidebar-list", ListView)
            labels = [renderable_text(item.children[0]) for item in lv.children]
            assert any("willow" in label for label in labels)
            assert not any("fern" in label for label in labels)

    asyncio.run(check())


def test_rename_awake_organism_swaps_to_new_path(nursery_app):
    """Renaming the awake organism moves its directory and keeps the app
    living with it — the old path must not be resurrected by a flush."""
    app = nursery_app

    async def check():
        async with app.run_test() as pilot:
            app._rename_org("default", "willow")
            await pilot.pause()
            assert app.org.dir_path.name == "willow"
            assert not (app.root / "organisms" / "default").exists()
            assert (app.root / "organisms" / "willow").is_dir()
            from replicanta import nursery as nursery_mod

            assert nursery_mod.current(app.root) == "willow"

    asyncio.run(check())


def test_menu_for_awake_organism_has_no_swap_option(nursery_app):
    from textual.widgets import OptionList

    from replicanta.tui import OrganismMenuScreen

    app = nursery_app

    async def check():
        async with app.run_test() as pilot:
            app._open_org_menu("default")
            await pilot.pause()
            assert isinstance(app.screen, OrganismMenuScreen)
            menu = app.screen.query_one(OptionList)
            ids = [option.id for option in menu.options]
            assert ids == ["rename", "group", "delete", "cancel"]
            # the awake organism can't be deleted: the app sits in its dir
            delete_option = next(o for o in menu.options if o.id == "delete")
            assert delete_option.disabled

    asyncio.run(check())


def test_menu_for_sleeping_organism_offers_enabled_delete(nursery_app):
    from textual.widgets import OptionList

    app = nursery_app
    _make_fern(app)

    async def check():
        async with app.run_test() as pilot:
            app._open_org_menu("fern")
            await pilot.pause()
            menu = app.screen.query_one(OptionList)
            delete_option = next(o for o in menu.options if o.id == "delete")
            assert not delete_option.disabled

    asyncio.run(check())


def test_delete_flow_asks_then_removes_organism(nursery_app):
    from replicanta import nursery as nursery_mod

    from replicanta.tui import ConfirmScreen

    app = nursery_app
    _make_fern(app)
    nursery_mod.create_group(app.root, "dreamers")
    nursery_mod.assign(app.root, "fern", "dreamers")

    async def check():
        async with app.run_test() as pilot:
            app._open_org_menu("fern")
            await pilot.pause()
            app.screen.dismiss(("delete", "fern"))
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)  # deletion confirms
            app.screen.dismiss(False)  # no → organism stays
            await pilot.pause()
            assert "fern" in nursery_mod.list_organisms(app.root)
            app._open_org_menu("fern")
            await pilot.pause()
            app.screen.dismiss(("delete", "fern"))
            await pilot.pause()
            app.screen.dismiss(True)  # yes → gone from disk, groups, sidebar
            await pilot.pause()
            assert "fern" not in nursery_mod.list_organisms(app.root)
            assert nursery_mod.load_groups(app.root) == {"dreamers": []}
            lv = app.query_one("#sidebar-list", ListView)
            labels = [renderable_text(item.children[0]) for item in lv.children]
            assert not any("fern" in label for label in labels)

    asyncio.run(check())


def test_delete_awake_organism_is_refused(nursery_app):
    from replicanta import nursery as nursery_mod

    from replicanta.tui import ConfirmScreen

    app = nursery_app

    async def check():
        async with app.run_test() as pilot:
            app._confirm_delete("default")
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmScreen)
            assert "default" in nursery_mod.list_organisms(app.root)

    asyncio.run(check())


def test_topbar_is_the_only_chrome_row(nursery_app):
    """Transcript-first shell: the single top-bar row is the only chrome.
    No tab bar, tabbed content, quick actions, command hints, or bottom bar."""
    from textual.widgets import TabbedContent

    app = nursery_app

    async def check():
        async with app.run_test(size=(160, 48)):
            assert app.query_one("#topbar", Static)
            assert len(app.query(TabbedContent)) == 0
            for removed in ("#tab-bar", "#quick-actions", "#command-hints", "#bottombar"):
                assert len(app.query(removed)) == 0, f"{removed} still in the DOM"
            # the transcript fills the content column
            content = app.query_one("#content")
            dreams = app.query_one("#dreams")
            assert dreams in list(content.children)
            chat = app.query_one("#chat")
            assert chat.styles.height.value == 3

    asyncio.run(check())


def test_status_line_shows_identity_and_state(nursery_app):
    """The top bar carries the whole status: organism identity, state,
    mood, the a/c/i readout, voice, and the clock."""
    app = nursery_app

    async def check():
        async with app.run_test(size=(160, 48)):
            app.refresh_top_bar()
            text = renderable_text(app.query_one("#topbar", Static), width=160)
            name = Path(app.org.dir_path).name
            assert name in text
            assert "awake" in text
            assert "calm" in text  # default mood
            assert "a/c/i" in text
            assert "voice" in text
            assert "UTC" in text
            assert app._topbar_text  # the compact mirror string stays maintained

    asyncio.run(check())


def test_top_bar_name_capped_at_fixed_budget(nursery_app, monkeypatch):
    """Regression ("blue bars move"): the learned name is free text with
    no cap, and a long name shoved the center/right grid cells — sometimes
    wrapping the bar onto a second line. The displayed name is truncated
    to a fixed budget and the outer columns are pinned, so the center
    readout stays put and the bar stays one row tall."""
    from replicanta.tui import TOPBAR_NAME_BUDGET

    app = nursery_app

    async def check():
        async with app.run_test(size=(120, 48)):
            offsets = []
            for name in ("bob", "x" * 60):
                monkeypatch.setattr(app, "_org_name", lambda name=name: name)
                app.refresh_top_bar()
                text = renderable_text(app.query_one("#topbar", Static), width=120).rstrip("\n")
                assert "\n" not in text, f"top bar wrapped with name {name!r}"
                assert "a/c/i" in text and "UTC" in text
                offsets.append(text.index("a/c/i"))
            assert "x" * 60 not in text
            assert "x" * (TOPBAR_NAME_BUDGET - 1) + "…" in text
            assert offsets[0] == offsets[1], f"center column moved: {offsets}"

    asyncio.run(check())


def test_top_bar_mirror_uses_stable_voice_string(nursery_app):
    """Regression: the change-detection string interpolated the voice
    MODULE (f"voice {voice}"), embedding a memory address-ish repr in the
    mirror text. It must carry the voice state string."""
    from replicanta import voice

    app = nursery_app

    async def check():
        async with app.run_test():
            app.refresh_top_bar()
            assert f"voice {voice.status()}" in app._topbar_text
            assert "<module" not in app._topbar_text

    asyncio.run(check())


def test_sidebar_labels_capped_to_one_line(nursery_app):
    """Long names must be truncated to the sidebar budget — an overflowing
    label would wrap its row onto a second line and misalign the list."""
    from replicanta.tui import SIDEBAR_LABEL_BUDGET

    app = nursery_app
    long_name = "pneumonoultramicroscopicsilicovolcanoconiosis"  # 45 chars
    (app.root / "organisms" / long_name).mkdir(parents=True)

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            lv = app.query_one("#sidebar-list", ListView)
            labels = [renderable_text(item.children[0]).rstrip("\n") for item in lv.children]
            for label in labels:
                assert len(label) <= SIDEBAR_LABEL_BUDGET, repr(label)
            long_row = next(label for label in labels if label.lstrip().startswith("p"))
            assert long_row.endswith("…")
            assert long_name not in long_row

    asyncio.run(check())


def test_sidebar_group_member_labels_capped_with_indent(nursery_app, monkeypatch):
    """Nested (group member) rows carry a 3-space indent, so their budget
    is smaller; the cap still applies (badges included)."""
    from replicanta import nursery as nursery_mod

    from replicanta.tui import SIDEBAR_LABEL_BUDGET

    app = nursery_app
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")
    nursery_mod.assign(app.root, "fern", "thinkers")
    # 6 badges × 2 chars = 12; "   ● fern" is 9 → 21 > 19, so the cap bites
    monkeypatch.setattr(app, "_badges_for_organism", lambda name: " 🪰 🦾 👁 💀 🧠 🦿")

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            lv = app.query_one("#sidebar-list", ListView)
            member = next(renderable_text(item.children[0]).rstrip("\n") for item in lv.children if item.name == "fern")
            assert len(member) <= SIDEBAR_LABEL_BUDGET - 3, repr(member)
            assert member.endswith("…")

    asyncio.run(check())


def test_ctrl_b_toggles_sidebar(nursery_app):
    app = nursery_app

    async def check():
        async with app.run_test(size=(120, 40)) as pilot:
            sidebar = app.query_one("#sidebar")
            assert sidebar.styles.display != "none"
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert sidebar.styles.display == "none"
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert sidebar.styles.display == "block"

    asyncio.run(check())


def test_narrow_terminal_hides_sidebar_on_mount(nursery_app):
    """Below 80 columns the shell starts transcript-first; ctrl+b reveals
    the nursery sidebar."""
    app = nursery_app

    async def check():
        async with app.run_test(size=(60, 24)) as pilot:
            sidebar = app.query_one("#sidebar")
            assert sidebar.styles.display == "none"
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert sidebar.styles.display == "block"

    asyncio.run(check())


def test_narrow_mode_helper_units():
    """The narrow-mode predicate is the single source for mount + resize."""
    from replicanta.tui import NARROW_WIDTH, _narrow_mode

    assert _narrow_mode(NARROW_WIDTH - 1) is True
    assert _narrow_mode(NARROW_WIDTH) is False
    assert _narrow_mode(NARROW_WIDTH + 40) is False


def test_resizing_terminal_reflows_sidebar(nursery_app):
    """Narrow mode is re-evaluated on resize: shrinking hides the sidebar,
    growing brings it back — no restart needed."""
    app = nursery_app

    async def check():
        async with app.run_test(size=(120, 40)) as pilot:
            sidebar = app.query_one("#sidebar")
            assert sidebar.styles.display != "none"
            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            assert sidebar.styles.display == "none"
            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            assert sidebar.styles.display != "none"

    asyncio.run(check())


def test_mouse_capture_enabled_by_default(nursery_app):
    """Menus, buttons, and drag-and-drop are click-driven, so mouse
    reporting must start on (ctrl+m toggles it off for native selection)."""
    app = nursery_app

    async def check():
        async with app.run_test():
            assert app._mouse_enabled is True

    asyncio.run(check())


def test_mind_memory_inner_live_in_being_overlay(nursery_app):
    """The former mind/memory/inner/cells tabs moved into the F3 overlay;
    the main screen DOM carries no pane widgets."""
    app = nursery_app

    async def check():
        async with app.run_test():
            for pane_widget in ("#mind", "#memory", "#inner", "#cells", "#visual", "#mud", "#doom"):
                assert len(app.query(pane_widget)) == 0, f"{pane_widget} leaked into the main screen"

    asyncio.run(check())


def test_chat_input_stays_below_main_area(nursery_app):
    app = nursery_app

    async def check():
        async with app.run_test():
            main = app.query_one("#main")
            chat = app.query_one("#chat")
            assert main.styles.height.value == 1.0  # 1fr
            assert chat.styles.height.value == 3

    asyncio.run(check())


def test_cells_section_click_opens_detail(nursery_app):
    """Left-clicking an occupied cell in the CELLS section of the being
    overlay opens the inspector with the object's kind and metadata."""
    from replicanta import tui_views
    from replicanta.tui import BeingScreen, CellDetailScreen

    app = nursery_app
    app.org.store.add(("cat", "has_fur", "true"), 0.9)

    async def check():
        async with app.run_test(size=(120, 60)) as pilot:
            app._refresh_views()
            app.action_being()
            await pilot.pause()
            assert isinstance(app.screen, BeingScreen)
            cells = app.screen.query_one("#cells", Static)
            cells.scroll_visible(animate=False)
            await pilot.pause()
            idx = next(i for i, c in enumerate(app._cells_grid) if c)
            row, col = divmod(idx, tui_views.CELLS_COLS)
            await pilot.click("#cells", offset=(col * 2, row + 1))
            await pilot.pause()
            assert isinstance(app.screen, CellDetailScreen)
            detail = renderable_text(app.screen.query_one("#cell-detail"))
            assert "kind: belief" in detail
            assert "object:    cat" in detail
            assert "attribute: has_fur" in detail
            # click anywhere closes the inspector
            await pilot.click("#cell-detail")
            await pilot.pause()
            assert not isinstance(app.screen, CellDetailScreen)

    asyncio.run(check())


def test_cells_section_click_on_empty_cell_does_nothing(nursery_app):
    from replicanta import tui_views
    from replicanta.tui import BeingScreen, CellDetailScreen

    app = nursery_app
    app.org.store.add(("cat", "has_fur", "true"), 0.9)

    async def check():
        async with app.run_test(size=(120, 60)) as pilot:
            app._refresh_views()
            app.action_being()
            await pilot.pause()
            assert isinstance(app.screen, BeingScreen)
            cells = app.screen.query_one("#cells", Static)
            cells.scroll_visible(animate=False)
            await pilot.pause()
            idx = next(i for i, c in enumerate(app._cells_grid) if not c)
            row, col = divmod(idx, tui_views.CELLS_COLS)
            await pilot.click("#cells", offset=(col * 2, row + 1))
            await pilot.pause()
            assert not isinstance(app.screen, CellDetailScreen)

    asyncio.run(check())


def test_actuation_command_toggles_with_default_on(nursery_app):
    """/actuation flips the persisted entity_actuation flag (default ON).
    The attribute may not exist on organisms that predate the schema, so
    the first toggle treats a missing flag as ON and lands it OFF."""
    app = nursery_app
    assert getattr(app.org, "entity_actuation", True) is True

    async def check():
        async with app.run_test():
            app.dispatch_command("/actuation")
            assert app.org.entity_actuation is False
            app.dispatch_command("/actuation")
            assert app.org.entity_actuation is True

    asyncio.run(check())


def test_actuation_command_logs_clear_status(nursery_app):
    app = nursery_app

    async def check():
        async with app.run_test():
            app.dispatch_command("/actuation")
            app.dispatch_command("/actuation")
            from textual.widgets import RichLog

            lines = [str(line.text) for line in app.query_one("#dreams", RichLog).lines]
            assert any("entity actuation OFF" in line for line in lines), lines
            assert any("entity actuation ON — entities may move/game" in line for line in lines), lines

    asyncio.run(check())


def test_actuation_registered_for_palette_and_api():
    """The COMMANDS table feeds the palette and /api/commands, so the
    toggle must be listed with a dispatch handler."""
    from replicanta import tui_commands

    entry = next((c for c in tui_commands.COMMANDS if c[0] == "/actuation"), None)
    assert entry is not None, "/actuation missing from COMMANDS"
    assert tui_commands.COMMAND_HANDLERS["/actuation"] is not None


# -- group chat (F-key-free wiring) ------------------------------------------


def test_group_command_start_status_and_stop(nursery_app):
    app = nursery_app
    _make_fern(app)

    async def check():
        async with app.run_test():
            app.dispatch_command("/group start fern")
            assert app._group is not None
            assert app._group.names() == ["default", "fern"]
            # the current organism participates as itself
            assert app._group.members["default"] is app.org
            app.dispatch_command("/group stop")
            assert app._group is None

    asyncio.run(check())


def test_group_command_rejects_unknown_and_solo(nursery_app):
    app = nursery_app

    async def check():
        async with app.run_test():
            app.dispatch_command("/group start ghost")
            assert app._group is None
            app.dispatch_command("/group start default")
            assert app._group is None  # a group needs two members

    asyncio.run(check())


def test_route_chat_message_in_group_mode_broadcasts(nursery_app, monkeypatch):
    """In group mode a chat line goes to the group broadcast worker, not
    the solo reply path, and it does not pollute individual chat logs."""
    from conftest import patch_generate

    from replicanta import groupchat

    app = nursery_app
    _make_fern(app)
    patch_generate(monkeypatch, lambda *a, **k: "hi from fern")

    async def check():
        async with app.run_test():
            app.dispatch_command("/group start fern")
            assert isinstance(app._group, groupchat.GroupChat)
            solo_calls = []
            monkeypatch.setattr(
                app,
                "_maybe_respond",
                lambda text: solo_calls.append(text),
            )
            app.route_chat_message("hello everyone")
            # wait for the background group respond worker (genuine
            # off-loop worker, so poll bounded rather than fixed-sleep)
            await wait_until(lambda: not app._group_responding, timeout=2.0, message="group respond worker to finish")
            assert solo_calls == []
            # every member remembers the line as a group episode, but it is
            # not recorded in their one-on-one chat_log.
            assert any(
                "hello everyone" in e["text"] for e in app._group.members["fern"].store.memory if e["kind"] == "group"
            )
            assert not any("hello everyone" in t for _r, t in app._group.members["fern"].store.chat_log)

    asyncio.run(check())


def test_group_deliver_renders_member_cards(nursery_app):
    app = nursery_app
    _make_fern(app)

    async def check():
        async with app.run_test() as pilot:
            app.dispatch_command("/group start fern")
            app._deliver_group([("fern", "hi from fern"), ("default", "hi from default")])
            await pilot.pause()
            # group replies are rendered as member cards, but they must not
            # pollute each speaker's individual one-on-one chat log.
            assert not any("hi from fern" in t for _r, t in app._group.members["fern"].store.chat_log)
            assert not any("hi from default" in t for _r, t in app.org.store.chat_log)

    asyncio.run(check())


def test_log_narration_records_musing_in_chat_log(nursery_app):
    """Idle musings enter the chat log so later prompts — and the
    cross-cycle repeat gate — know what the voice already said."""
    app = nursery_app

    async def check():
        async with app.run_test():
            app._log_narration("a quiet thought about rain.")
            assert any("a quiet thought about rain." in t for _r, t in app.org.store.chat_log)

    asyncio.run(check())


# -- nursery groups in the sidebar -------------------------------------------


def test_sidebar_renders_groups_with_members(nursery_app):
    from replicanta import nursery as nursery_mod

    app = nursery_app
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")
    nursery_mod.assign(app.root, "fern", "thinkers")

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            lv = app.query_one("#sidebar-list", ListView)
            entries = [(item.name, renderable_text(item.children[0])) for item in lv.children]
            names = [n for n, _label in entries]
            # group header present, fern nested under it, default stays flat
            assert "group:thinkers" in names
            assert names.index("group:thinkers") < names.index("fern")
            header = next(label for n, label in entries if n == "group:thinkers")
            assert "▾ thinkers" in header
            member = next(label for n, label in entries if n == "fern")
            assert member.startswith("   ")  # indented under the header

    asyncio.run(check())


def test_group_header_selection_opens_group_menu(nursery_app):
    from replicanta import nursery as nursery_mod
    from replicanta.tui import GroupMenuScreen

    app = nursery_app
    nursery_mod.create_group(app.root, "thinkers")

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            lv = app.query_one("#sidebar-list", ListView)
            header = next(item for item in lv.children if item.name == "group:thinkers")
            index = list(lv.children).index(header)
            app.on_list_view_selected(ListView.Selected(lv, header, index))
            await pilot.pause()
            assert isinstance(app.screen, GroupMenuScreen)

    asyncio.run(check())


def test_rename_group_flow_updates_disk_and_sidebar(nursery_app):
    from replicanta import nursery as nursery_mod

    app = nursery_app
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")
    nursery_mod.assign(app.root, "fern", "thinkers")

    async def check():
        async with app.run_test() as pilot:
            app._prompt_rename_group("thinkers")
            await pilot.pause()
            app.screen.dismiss("dreamers")
            await pilot.pause()
            assert nursery_mod.load_groups(app.root) == {"dreamers": ["fern"]}
            lv = app.query_one("#sidebar-list", ListView)
            names = [item.name for item in lv.children]
            assert "group:dreamers" in names
            assert "group:thinkers" not in names

    asyncio.run(check())


def test_organism_menu_move_to_group_assigns(nursery_app):
    from replicanta import nursery as nursery_mod

    app = nursery_app
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")

    async def check():
        from replicanta.tui import GroupPickScreen

        async with app.run_test() as pilot:
            app._open_org_menu("fern")
            await pilot.pause()
            app.screen.dismiss(("group", "fern"))
            await pilot.pause()
            assert isinstance(app.screen, GroupPickScreen)
            app.screen.dismiss("thinkers")
            await pilot.pause()
            assert nursery_mod.group_of(app.root, "fern") == "thinkers"

    asyncio.run(check())


def test_group_pick_new_group_creates_and_assigns(nursery_app):
    from replicanta import nursery as nursery_mod

    app = nursery_app
    _make_fern(app)

    async def check():
        from replicanta.tui import NamePromptScreen

        async with app.run_test() as pilot:
            app._pick_group_for("fern")
            await pilot.pause()
            app.screen.dismiss("new")
            await pilot.pause()
            assert isinstance(app.screen, NamePromptScreen)
            app.screen.dismiss("fresh group")
            await pilot.pause()
            assert nursery_mod.group_of(app.root, "fern") == "fresh group"

    asyncio.run(check())


def test_right_click_group_header_opens_rename_prompt(nursery_app):
    from replicanta import nursery as nursery_mod
    from replicanta.tui import NamePromptScreen

    app = nursery_app
    nursery_mod.create_group(app.root, "thinkers")

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            lv = app.query_one("#sidebar-list", ListView)
            header = next(item for item in lv.children if item.name == "group:thinkers")
            # right-click the header row (offset is screen-relative here:
            # no widget selector, so pilot aims at the screen itself)
            await pilot.click(None, offset=(header.region.x + 2, header.region.y), button=3)
            await pilot.pause()
            assert isinstance(app.screen, NamePromptScreen)

    asyncio.run(check())


def test_right_click_empty_sidebar_opens_new_group_prompt(nursery_app):
    from replicanta.tui import NamePromptScreen

    app = nursery_app

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            # below the single organism row = empty sidebar space
            await pilot.click("#sidebar-list", offset=(2, 6), button=3)
            await pilot.pause()
            assert isinstance(app.screen, NamePromptScreen)

    asyncio.run(check())


# -- drag and drop into groups ------------------------------------------------


def _sidebar_regions(app):
    lv = app.query_one("#sidebar-list", ListView)
    return {item.name: item.region for item in lv.children}


def test_drag_organism_onto_group_header_assigns(nursery_app):
    from replicanta import nursery as nursery_mod

    app = nursery_app
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            regions = _sidebar_regions(app)
            src = regions["fern"]
            dst = regions["group:thinkers"]
            await pilot.mouse_down(None, offset=(src.x + 2, src.y))
            await pilot.hover(None, offset=(dst.x + 2, dst.y))
            await pilot.mouse_up(None, offset=(dst.x + 2, dst.y))
            await pilot.pause()
            assert nursery_mod.group_of(app.root, "fern") == "thinkers"

    asyncio.run(check())


def test_drag_organism_onto_group_member_assigns(nursery_app):
    from replicanta import nursery as nursery_mod

    app = nursery_app
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")
    nursery_mod.assign(app.root, "default", "thinkers")

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            regions = _sidebar_regions(app)
            src = regions["fern"]
            dst = regions["default"]  # already a member of thinkers
            await pilot.mouse_down(None, offset=(src.x + 2, src.y))
            await pilot.hover(None, offset=(dst.x + 2, dst.y))
            await pilot.mouse_up(None, offset=(dst.x + 2, dst.y))
            await pilot.pause()
            assert nursery_mod.group_of(app.root, "fern") == "thinkers"

    asyncio.run(check())


def test_drag_member_onto_empty_space_ungroups(nursery_app):
    from replicanta import nursery as nursery_mod

    app = nursery_app
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")
    nursery_mod.assign(app.root, "fern", "thinkers")

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            regions = _sidebar_regions(app)
            src = regions["fern"]
            lv = app.query_one("#sidebar-list", ListView)
            empty_y = lv.region.y + 8  # below every row
            await pilot.mouse_down(None, offset=(src.x + 2, src.y))
            await pilot.hover(None, offset=(2, empty_y))
            await pilot.mouse_up(None, offset=(2, empty_y))
            await pilot.pause()
            assert nursery_mod.group_of(app.root, "fern") is None

    asyncio.run(check())


def test_plain_click_does_not_become_a_drag(nursery_app):
    """A plain left click without movement swaps to the organism and never
    assigns anything to a group."""
    from replicanta import nursery as nursery_mod
    from textual.screen import ModalScreen

    app = nursery_app
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            regions = _sidebar_regions(app)
            src = regions["fern"]
            await pilot.click(None, offset=(src.x + 2, src.y))
            await pilot.pause()
            assert app.org.dir_path.name == "fern"
            assert not isinstance(app.screen, ModalScreen)  # no menu
            assert nursery_mod.group_of(app.root, "fern") is None

    asyncio.run(check())


def test_group_command_start_expands_nursery_groups(nursery_app):
    """'/group start <groupname>' seats every member of the nursery group."""
    from replicanta import nursery as nursery_mod

    app = nursery_app
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")
    nursery_mod.assign(app.root, "fern", "thinkers")

    async def check():
        async with app.run_test():
            app.dispatch_command("/group start thinkers")
            assert set(app._group.names()) == {"default", "fern"}
            assert app._group.members["default"] is app.org

    asyncio.run(check())


def test_group_command_start_organism_beats_same_named_group(nursery_app):
    """When an organism and a group share a name, the organism wins."""
    from replicanta import nursery as nursery_mod

    app = nursery_app
    _make_fern(app)
    nursery_mod.create_group(app.root, "fern")  # group named like the org
    nursery_mod.assign(app.root, "default", "fern")

    async def check():
        async with app.run_test():
            app.dispatch_command("/group start fern")
            assert set(app._group.names()) == {"default", "fern"}
            # fern itself was seated, not expanded from the group
            assert "fern" in app._group.members

    asyncio.run(check())


def test_group_command_unknown_group_reports(nursery_app):
    app = nursery_app

    async def check():
        async with app.run_test():
            app.dispatch_command("/group start nowhere")
            assert app._group is None

    asyncio.run(check())


# -- mud turn-generation race ------------------------------------------------


def test_mud_stale_organism_move_dropped_after_user_move(nursery_app):
    """A move chosen before the user's command must not land after it:
    the generation captured when the choice worker started no longer
    matches once the user acts."""
    from replicanta import mud as mud_mod

    app = nursery_app

    async def check():
        async with app.run_test():
            game = mud_mod.MudGame()
            app._mud.game = game
            stale_gen = app._mud.turn_gen  # in-flight move started here
            app.route_chat_message("go north")
            assert game.turns == 1  # user move applied instantly
            assert app._mud.turn_gen == stale_gen + 1
            # the late organism move arrives — the world has moved on
            app._mud.apply(game, "go south", gen=stale_gen)
            assert game.turns == 1  # dropped, not applied

    asyncio.run(check())


def test_mud_stale_organism_move_dropped_after_hint(nursery_app, monkeypatch):
    """A typed hint (not a command) also invalidates an in-flight move —
    the hint was meant for the next choice, not the one already made."""
    from replicanta import mud as mud_mod

    app = nursery_app
    monkeypatch.setattr(app, "_maybe_respond", lambda text: None)

    async def check():
        async with app.run_test():
            game = mud_mod.MudGame()
            app._mud.game = game
            stale_gen = app._mud.turn_gen
            app.route_chat_message("maybe try the door")
            assert app._mud.turn_gen == stale_gen + 1
            assert app._mud.hint == "maybe try the door"
            app._mud.apply(game, "go south", gen=stale_gen)
            assert game.turns == 0

    asyncio.run(check())


def test_mud_fresh_organism_move_still_applies(nursery_app):
    """A move chosen at the current generation applies normally."""
    from replicanta import mud as mud_mod

    app = nursery_app

    async def check():
        async with app.run_test():
            game = mud_mod.MudGame()
            app._mud.game = game
            app._mud.apply(game, "look", gen=app._mud.turn_gen)
            assert game.turns == 1

    asyncio.run(check())


def test_mud_organism_move_shows_its_reason(nursery_app):
    """The organism's stated reason is logged (dim) just before its
    command, so a move never appears unmotivated."""
    from textual.widgets import RichLog

    from replicanta import mud as mud_mod

    app = nursery_app

    async def check():
        async with app.run_test():
            game = mud_mod.MudGame()
            app._mud.game = game
            app._mud.apply(
                game,
                "go north",
                gen=app._mud.turn_gen,
                reason="because the cave mouth calls",
            )
            lines = [str(line.text) for line in app.query_one("#dreams", RichLog).lines]
            idx = next(i for i, line in enumerate(lines) if "cave mouth calls" in line)
            assert "> go north" in lines[idx + 1]

    asyncio.run(check())


def test_swap_ends_active_group_chat(nursery_app):
    """Swapping organisms must end an active group chat: the group keeps
    the old (closed) organism object and would keep broadcasting against
    unpersisted state."""

    from textual.widgets import RichLog

    app = nursery_app
    _make_fern(app)

    async def check():
        async with app.run_test():
            app._group_command(["start", "fern"])
            assert app._group is not None, "group chat did not start"
            app._swap_to("fern")
            assert app._group is None, "swap left the stale group chat active"
            assert app.org.dir_path.name == "fern"
            lines = [str(line.text) for line in app.query_one("#dreams", RichLog).lines]
            assert any("group chat ended" in line for line in lines), lines

    asyncio.run(check())


def test_swap_without_group_is_quiet(nursery_app):
    """A swap with no active group chat must not log a group-ended line."""

    from textual.widgets import RichLog

    app = nursery_app
    _make_fern(app)

    async def check():
        async with app.run_test():
            app._swap_to("fern")
            assert app._group is None
            lines = [str(line.text) for line in app.query_one("#dreams", RichLog).lines]
            assert not any("group chat ended" in line for line in lines), lines

    asyncio.run(check())


def test_doom_frame_keeps_fixed_width_and_scrolls(nursery_app):
    """The 80-column game frame must never wrap: on narrow terminals the
    overlay scrolls horizontally instead of shredding the ASCII art."""
    from textual.containers import ScrollableContainer

    from replicanta.tui import DoomScreen

    app = nursery_app

    async def check():
        async with app.run_test(size=(60, 24)) as pilot:
            app.push_screen(DoomScreen())
            await pilot.pause()
            doom = app.screen.query_one("#doom", Static)
            await pilot.pause()
            # 82 outer - 2 padding = exactly the 80-column frame: any
            # narrower content region wraps the art mid-line.
            assert doom.styles.width is not None and doom.styles.width.value == 82
            assert doom.region.width == 82
            assert doom.content_region.width == 80
            assert isinstance(doom.parent, ScrollableContainer)
            # esc closes the overlay; the (non-)game is untouched
            await pilot.press("escape")
            await pilot.pause()
            assert type(app.screen).__name__ == "Screen"

    asyncio.run(check())


def test_user_chat_while_busy_is_queued_then_answered(nursery_app):
    """Forced intervention: a user line that arrives while the entity is
    mid-thought (chat reply or doom auto-play turn) must not vanish — it is
    held and answered the moment the current thought lands."""
    app = nursery_app
    calls = []
    app._respond = lambda text, *, quick=False, temperature=None: calls.append(text)

    async def check():
        async with app.run_test() as pilot:
            app._responding = True  # entity mid-thought
            app._maybe_respond("hello?")
            await pilot.pause()
            assert calls == []  # not answered yet, but not dropped either
            assert app._queued_prompt is not None
            app._responding = False  # the in-flight thought lands
            app._drain_queued_prompt()
            await pilot.pause()
            assert calls == ["hello?"]

    asyncio.run(check())


def test_queued_prompt_keeps_latest_only(nursery_app):
    """While busy, only the most recent user line is held — answering a
    stale backlog would read worse than answering the latest intent."""
    app = nursery_app
    calls = []
    app._respond = lambda text, *, quick=False, temperature=None: calls.append(text)

    async def check():
        async with app.run_test() as pilot:
            app._responding = True
            app._maybe_respond("first")
            app._maybe_respond("second")
            await pilot.pause()
            app._responding = False
            app._drain_queued_prompt()
            await pilot.pause()
            assert calls == ["second"]

    asyncio.run(check())


def test_doom_turn_done_drains_queued_prompt(nursery_app):
    """The end of a doom auto-play turn answers a user line queued during
    the generation — a running game must never swallow chat."""
    app = nursery_app
    calls = []
    app._respond = lambda text, *, quick=False, temperature=None: calls.append(text)

    async def check():
        async with app.run_test() as pilot:
            app._responding = True  # the turn held the flag
            app._maybe_respond("are you there?")
            app._doom._turn_done()
            await pilot.pause()
            # the queued line is answered; the flag is held again by that
            # answer's own generation
            assert calls == ["are you there?"]
            assert app._responding is True

    asyncio.run(check())


def test_respond_watchdog_releases_stuck_flag(nursery_app, monkeypatch):
    """A generation that never returns (wedged worker, dead backend) must
    not silence the entity until restart: the 1s-tick watchdog frees the
    busy flag past 600s and answers anything queued during the wedge."""
    import time as time_mod

    app = nursery_app
    calls = []
    app._respond = lambda text, *, quick=False, temperature=None: calls.append(text)

    async def check():
        async with app.run_test() as pilot:
            app._responding = True
            app._respond_started = time_mod.monotonic() - 700  # wedged long ago
            app._maybe_respond("anyone home?")  # queued during the wedge
            app._respond_watchdog()
            await pilot.pause()
            assert app._responding is True  # the queued answer now holds it
            assert calls == ["anyone home?"]
            app._responding = False

    asyncio.run(check())


def test_respond_watchdog_ignores_fresh_generation(nursery_app):
    """A healthy in-flight generation is far below the watchdog threshold
    and must never be interrupted."""
    import time as time_mod

    app = nursery_app

    async def check():
        async with app.run_test():
            app._responding = True
            app._respond_started = time_mod.monotonic()
            app._respond_watchdog()
            assert app._responding is True  # untouched
            app._responding = False

    asyncio.run(check())


def test_stale_worker_cannot_release_newer_generation(nursery_app):
    """The gen guard: a late finally from a wedged worker (already replaced
    by the watchdog and a new generation) must not release the new one."""
    app = nursery_app
    calls = []
    app._respond = lambda text, *, quick=False, temperature=None: calls.append(text)

    async def check():
        async with app.run_test() as pilot:
            app._respond_gen = 7
            app._responding = True  # the new generation's flag
            app._release_respond(6)  # the stale worker's finally
            await pilot.pause()
            assert app._responding is True  # refused: stale worker
            assert calls == []
            app._release_respond(7)  # the current worker's finally
            await pilot.pause()
            assert app._responding is False

    asyncio.run(check())
