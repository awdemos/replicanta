"""Layout regression tests for the workspace chrome."""

import asyncio
from pathlib import Path

from textual.containers import VerticalScroll
from textual.widgets import Button, ListView, Static

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
            assert "a/r/i" in text  # mental-state readout in the center zone
            assert "UTC" in text  # clock in the right zone

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


def test_sidebar_selection_opens_action_menu(nursery_app):
    """Left-click / Enter on a sidebar organism opens its dropdown menu
    instead of swapping immediately."""
    from replicanta.tui import OrganismMenuScreen

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
            assert isinstance(app.screen, OrganismMenuScreen)
            assert app.org.dir_path.name == "default"  # not swapped yet

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
            assert ids == ["rename", "group", "cancel"]

    asyncio.run(check())


def test_bottom_bar_shows_counts_and_keys(nursery_app):
    app = nursery_app

    async def check():
        async with app.run_test():
            app.refresh_status()
            text = renderable_text(app.query_one("#bottombar-text", Static), width=120)
            assert "beliefs" in text
            assert "rules" in text
            assert "ctrl+p" in text
            assert "F1" in text
            assert "ctrl+q quit" in text
            assert text.strip() == app._bottombar_text

    asyncio.run(check())


def test_mind_memory_inner_are_scrollable(nursery_app):
    app = nursery_app

    async def check():
        async with app.run_test():
            for pane in ("mind-pane", "memory-pane", "inner-pane"):
                tab = app.query_one(f"#{pane}")
                scroll = tab.query_one(VerticalScroll)
                assert scroll is not None

    asyncio.run(check())


def test_chat_input_stays_below_main_area(nursery_app):
    app = nursery_app

    async def check():
        async with app.run_test():
            main = app.query_one("#main")
            chat = app.query_one("#chat")
            bottom = app.query_one("#bottombar")
            assert main.styles.height.value == 1.0  # 1fr
            assert chat.styles.height.value == 3
            assert bottom.styles.height.value == 1

    asyncio.run(check())


def test_cells_tab_click_opens_detail(nursery_app):
    """Left-clicking an occupied cell in the F8 grid opens the inspector
    with the object's kind and metadata."""
    from replicanta import tui_views
    from replicanta.tui import CellDetailScreen

    app = nursery_app
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
            detail = renderable_text(app.screen.query_one("#cell-detail"))
            assert "kind: belief" in detail
            assert "object:    cat" in detail
            assert "attribute: has_fur" in detail
            # click anywhere closes the inspector
            await pilot.click("#cell-detail")
            await pilot.pause()
            assert not isinstance(app.screen, CellDetailScreen)

    asyncio.run(check())


def test_cells_tab_click_on_empty_cell_does_nothing(nursery_app):
    from replicanta import tui_views
    from replicanta.tui import CellDetailScreen

    app = nursery_app
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
    """A left click without movement still opens the action menu and
    never assigns anything."""
    from replicanta import nursery as nursery_mod
    from replicanta.tui import OrganismMenuScreen

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
            assert isinstance(app.screen, OrganismMenuScreen)
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
            app._mud_game = game
            stale_gen = app._mud_turn_gen  # in-flight move started here
            app.route_chat_message("go north")
            assert game.turns == 1  # user move applied instantly
            assert app._mud_turn_gen == stale_gen + 1
            # the late organism move arrives — the world has moved on
            app._mud_apply(game, "go south", gen=stale_gen)
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
            app._mud_game = game
            stale_gen = app._mud_turn_gen
            app.route_chat_message("maybe try the door")
            assert app._mud_turn_gen == stale_gen + 1
            assert app._mud_hint == "maybe try the door"
            app._mud_apply(game, "go south", gen=stale_gen)
            assert game.turns == 0

    asyncio.run(check())


def test_mud_fresh_organism_move_still_applies(nursery_app):
    """A move chosen at the current generation applies normally."""
    from replicanta import mud as mud_mod

    app = nursery_app

    async def check():
        async with app.run_test():
            game = mud_mod.MudGame()
            app._mud_game = game
            app._mud_apply(game, "look", gen=app._mud_turn_gen)
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
            app._mud_game = game
            app._mud_apply(
                game,
                "go north",
                gen=app._mud_turn_gen,
                reason="because the cave mouth calls",
            )
            lines = [str(line.text) for line in app.query_one("#dreams", RichLog).lines]
            idx = next(i for i, line in enumerate(lines) if "cave mouth calls" in line)
            assert "> go north" in lines[idx + 1]

    asyncio.run(check())


def test_quick_actions_buttons_exist(nursery_app):
    """The sidebar must expose one-click action buttons."""
    app = nursery_app

    async def check():
        async with app.run_test():
            for bid in ("qa-sleep", "qa-voice", "qa-listen", "qa-look", "qa-mud"):
                assert app.query_one(f"#{bid}", Button)

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
