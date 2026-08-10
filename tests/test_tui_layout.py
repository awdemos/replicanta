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
            assert ids == ["rename", "group", "cancel"]

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


def test_cells_tab_click_opens_detail(monkeypatch, tmp_path):
    """Left-clicking an occupied cell in the F8 grid opens the inspector
    with the object's kind and metadata."""
    import tui_views
    from tui import CellDetailScreen
    app = _headless_app(monkeypatch, tmp_path)
    app.org.store.add(("cat", "has_fur", "true"), 0.9)

    async def check():
        async with app.run_test() as pilot:
            app._refresh_views()
            app.action_show_tab("cells-pane")
            await pilot.pause()
            idx = next(i for i, c in enumerate(app._cells_grid) if c)
            row, col = divmod(idx, tui_views.CELLS_COLS)
            await pilot.click("#cells", offset=(col * 2, row + 1))
            await pilot.pause()
            assert isinstance(app.screen, CellDetailScreen)
            detail = _renderable_text(app.screen.query_one("#cell-detail"))
            assert "kind: belief" in detail
            assert "object:    cat" in detail
            assert "attribute: has_fur" in detail
            # click anywhere closes the inspector
            await pilot.click("#cell-detail")
            await pilot.pause()
            assert not isinstance(app.screen, CellDetailScreen)

    asyncio.run(check())


def test_cells_tab_click_on_empty_cell_does_nothing(monkeypatch, tmp_path):
    import tui_views
    from tui import CellDetailScreen
    app = _headless_app(monkeypatch, tmp_path)
    app.org.store.add(("cat", "has_fur", "true"), 0.9)

    async def check():
        async with app.run_test() as pilot:
            app._refresh_views()
            app.action_show_tab("cells-pane")
            await pilot.pause()
            idx = next(i for i, c in enumerate(app._cells_grid) if not c)
            row, col = divmod(idx, tui_views.CELLS_COLS)
            await pilot.click("#cells", offset=(col * 2, row + 1))
            await pilot.pause()
            assert not isinstance(app.screen, CellDetailScreen)

    asyncio.run(check())


# -- group chat (F-key-free wiring) ------------------------------------------

def test_group_command_start_status_and_stop(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)

    async def check():
        async with app.run_test():
            app.handle_command("/group start fern")
            assert app._group is not None
            assert app._group.names() == ["default", "fern"]
            # the current organism participates as itself
            assert app._group.members["default"] is app.org
            app.handle_command("/group stop")
            assert app._group is None

    asyncio.run(check())


def test_group_command_rejects_unknown_and_solo(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            app.handle_command("/group start ghost")
            assert app._group is None
            app.handle_command("/group start default")
            assert app._group is None  # a group needs two members

    asyncio.run(check())


def test_handle_chat_in_group_mode_broadcasts(monkeypatch, tmp_path):
    """In group mode a chat line goes to the group broadcast worker, not
    the solo reply path."""
    import groupchat
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)

    async def check():
        async with app.run_test():
            app.handle_command("/group start fern")
            assert isinstance(app._group, groupchat.GroupChat)
            calls = {"group": 0, "solo": 0}
            monkeypatch.setattr(app, "_maybe_group_respond",
                                lambda text: calls.__setitem__(
                                    "group", calls["group"] + 1))
            monkeypatch.setattr(app, "_maybe_respond",
                                lambda text: calls.__setitem__(
                                    "solo", calls["solo"] + 1))
            app.handle_chat("hello everyone")
            assert calls == {"group": 1, "solo": 0}
            # every member heard the line
            assert any("hello everyone" in t
                       for _r, t in app._group.members["fern"]
                       .store.chat_log)

    asyncio.run(check())


def test_group_deliver_renders_member_cards(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)

    async def check():
        async with app.run_test():
            app.handle_command("/group start fern")
            app._deliver_group([("fern", "hi from fern"),
                                ("default", "hi from default")])
            await asyncio.sleep(0.05)
            # replies recorded into each speaker's own chat log
            assert any("hi from fern" in t
                       for _r, t in app._group.members["fern"]
                       .store.chat_log)

    asyncio.run(check())


def test_log_narration_records_musing_in_chat_log(monkeypatch, tmp_path):
    """Idle musings enter the chat log so later prompts — and the
    cross-cycle repeat gate — know what the voice already said."""
    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            app._log_narration("a quiet thought about rain.")
            assert any("a quiet thought about rain." in t
                       for _r, t in app.org.store.chat_log)

    asyncio.run(check())


# -- nursery groups in the sidebar -------------------------------------------

def test_sidebar_renders_groups_with_members(monkeypatch, tmp_path):
    import nursery as nursery_mod
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")
    nursery_mod.assign(app.root, "fern", "thinkers")

    async def check():
        async with app.run_test():
            app._refresh_sidebar()
            await asyncio.sleep(0.05)
            lv = app.query_one("#sidebar-list", ListView)
            entries = [(item.name, str(_renderable_text(item.children[0])))
                       for item in lv.children]
            names = [n for n, _label in entries]
            # group header present, fern nested under it, default stays flat
            assert "group:thinkers" in names
            assert names.index("group:thinkers") < names.index("fern")
            header = next(label for n, label in entries
                          if n == "group:thinkers")
            assert "▾ thinkers" in header
            member = next(label for n, label in entries if n == "fern")
            assert member.startswith("   ")  # indented under the header

    asyncio.run(check())


def test_group_header_selection_opens_group_menu(monkeypatch, tmp_path):
    import nursery as nursery_mod
    from tui import GroupMenuScreen
    app = _headless_app(monkeypatch, tmp_path)
    nursery_mod.create_group(app.root, "thinkers")

    async def check():
        async with app.run_test():
            app._refresh_sidebar()
            await asyncio.sleep(0.05)
            lv = app.query_one("#sidebar-list", ListView)
            header = next(item for item in lv.children
                          if item.name == "group:thinkers")
            event = type("Selected", (), {"item": header})()
            app.on_list_view_selected(event)
            await asyncio.sleep(0.05)
            assert isinstance(app.screen, GroupMenuScreen)

    asyncio.run(check())


def test_rename_group_flow_updates_disk_and_sidebar(monkeypatch, tmp_path):
    import nursery as nursery_mod
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")
    nursery_mod.assign(app.root, "fern", "thinkers")

    async def check():
        async with app.run_test():
            app._prompt_rename_group("thinkers")
            await asyncio.sleep(0.05)
            app.screen.dismiss("dreamers")
            await asyncio.sleep(0.05)
            assert nursery_mod.load_groups(app.root) == {"dreamers": ["fern"]}
            lv = app.query_one("#sidebar-list", ListView)
            names = [item.name for item in lv.children]
            assert "group:dreamers" in names
            assert "group:thinkers" not in names

    asyncio.run(check())


def test_organism_menu_move_to_group_assigns(monkeypatch, tmp_path):
    import nursery as nursery_mod
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")

    async def check():
        async with app.run_test():
            app._open_org_menu("fern")
            await asyncio.sleep(0.05)
            app.screen.dismiss(("group", "fern"))
            await asyncio.sleep(0.05)
            from tui import GroupPickScreen
            assert isinstance(app.screen, GroupPickScreen)
            app.screen.dismiss("thinkers")
            await asyncio.sleep(0.05)
            assert nursery_mod.group_of(app.root, "fern") == "thinkers"

    asyncio.run(check())


def test_group_pick_new_group_creates_and_assigns(monkeypatch, tmp_path):
    import nursery as nursery_mod
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)

    async def check():
        async with app.run_test():
            app._pick_group_for("fern")
            await asyncio.sleep(0.05)
            app.screen.dismiss("new")
            await asyncio.sleep(0.05)
            from tui import NamePromptScreen
            assert isinstance(app.screen, NamePromptScreen)
            app.screen.dismiss("fresh group")
            await asyncio.sleep(0.05)
            assert nursery_mod.group_of(app.root, "fern") == "fresh group"

    asyncio.run(check())


def test_right_click_group_header_opens_rename_prompt(monkeypatch, tmp_path):
    import nursery as nursery_mod
    from tui import NamePromptScreen
    app = _headless_app(monkeypatch, tmp_path)
    nursery_mod.create_group(app.root, "thinkers")

    async def check():
        async with app.run_test() as pilot:
            app._refresh_sidebar()
            await pilot.pause()
            lv = app.query_one("#sidebar-list", ListView)
            header = next(item for item in lv.children
                          if item.name == "group:thinkers")
            # right-click the header row (offset is screen-relative here:
            # no widget selector, so pilot aims at the screen itself)
            await pilot.click(None,
                              offset=(header.region.x + 2,
                                      header.region.y),
                              button=3)
            await pilot.pause()
            assert isinstance(app.screen, NamePromptScreen)

    asyncio.run(check())


def test_right_click_empty_sidebar_opens_new_group_prompt(monkeypatch,
                                                          tmp_path):
    from tui import NamePromptScreen
    app = _headless_app(monkeypatch, tmp_path)

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


def test_drag_organism_onto_group_header_assigns(monkeypatch, tmp_path):
    import nursery as nursery_mod
    app = _headless_app(monkeypatch, tmp_path)
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


def test_drag_organism_onto_group_member_assigns(monkeypatch, tmp_path):
    import nursery as nursery_mod
    app = _headless_app(monkeypatch, tmp_path)
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


def test_drag_member_onto_empty_space_ungroups(monkeypatch, tmp_path):
    import nursery as nursery_mod
    app = _headless_app(monkeypatch, tmp_path)
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
            empty_y = lv.region.y + 8   # below every row
            await pilot.mouse_down(None, offset=(src.x + 2, src.y))
            await pilot.hover(None, offset=(2, empty_y))
            await pilot.mouse_up(None, offset=(2, empty_y))
            await pilot.pause()
            assert nursery_mod.group_of(app.root, "fern") is None

    asyncio.run(check())


def test_plain_click_does_not_become_a_drag(monkeypatch, tmp_path):
    """A left click without movement still opens the action menu and
    never assigns anything."""
    import nursery as nursery_mod
    from tui import OrganismMenuScreen
    app = _headless_app(monkeypatch, tmp_path)
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
            assert isinstance(app.screen, OrganismMenuScreen)
            assert nursery_mod.group_of(app.root, "fern") is None

    asyncio.run(check())


def test_group_command_start_expands_nursery_groups(monkeypatch, tmp_path):
    """'/group start <groupname>' seats every member of the nursery group."""
    import nursery as nursery_mod
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)
    nursery_mod.create_group(app.root, "thinkers")
    nursery_mod.assign(app.root, "fern", "thinkers")

    async def check():
        async with app.run_test():
            app.handle_command("/group start thinkers")
            assert set(app._group.names()) == {"default", "fern"}
            assert app._group.members["default"] is app.org

    asyncio.run(check())


def test_group_command_start_organism_beats_same_named_group(
        monkeypatch, tmp_path):
    """When an organism and a group share a name, the organism wins."""
    import nursery as nursery_mod
    app = _headless_app(monkeypatch, tmp_path)
    _make_fern(app)
    nursery_mod.create_group(app.root, "fern")  # group named like the org
    nursery_mod.assign(app.root, "default", "fern")

    async def check():
        async with app.run_test():
            app.handle_command("/group start fern")
            assert set(app._group.names()) == {"default", "fern"}
            # fern itself was seated, not expanded from the group
            assert "fern" in app._group.members

    asyncio.run(check())


def test_group_command_unknown_group_reports(monkeypatch, tmp_path):
    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            app.handle_command("/group start nowhere")
            assert app._group is None

    asyncio.run(check())


# -- mud turn-generation race ------------------------------------------------

def test_mud_stale_organism_move_dropped_after_user_move(monkeypatch,
                                                         tmp_path):
    """A move chosen before the user's command must not land after it:
    the generation captured when the choice worker started no longer
    matches once the user acts."""
    import mud as mud_mod
    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            game = mud_mod.MudGame()
            app._mud_game = game
            stale_gen = app._mud_turn_gen   # in-flight move started here
            app.handle_chat("go north")
            assert game.turns == 1          # user move applied instantly
            assert app._mud_turn_gen == stale_gen + 1
            # the late organism move arrives — the world has moved on
            app._mud_apply(game, "go south", gen=stale_gen)
            assert game.turns == 1          # dropped, not applied

    asyncio.run(check())


def test_mud_stale_organism_move_dropped_after_hint(monkeypatch, tmp_path):
    """A typed hint (not a command) also invalidates an in-flight move —
    the hint was meant for the next choice, not the one already made."""
    import mud as mud_mod
    app = _headless_app(monkeypatch, tmp_path)
    monkeypatch.setattr(app, "_maybe_respond", lambda text: None)

    async def check():
        async with app.run_test():
            game = mud_mod.MudGame()
            app._mud_game = game
            stale_gen = app._mud_turn_gen
            app.handle_chat("maybe try the door")
            assert app._mud_turn_gen == stale_gen + 1
            assert app._mud_hint == "maybe try the door"
            app._mud_apply(game, "go south", gen=stale_gen)
            assert game.turns == 0

    asyncio.run(check())


def test_mud_fresh_organism_move_still_applies(monkeypatch, tmp_path):
    """A move chosen at the current generation applies normally."""
    import mud as mud_mod
    app = _headless_app(monkeypatch, tmp_path)

    async def check():
        async with app.run_test():
            game = mud_mod.MudGame()
            app._mud_game = game
            app._mud_apply(game, "look", gen=app._mud_turn_gen)
            assert game.turns == 1

    asyncio.run(check())
