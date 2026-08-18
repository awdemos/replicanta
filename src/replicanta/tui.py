"""The Textual app shell: OrganismApp wires the organism core (organism.py),
the thought arena (arena.py) and the MUD engine (mud.py) to the terminal,
delegating pure rendering/parsing to tui_views.py and tui_commands.py."""

import json
import logging
import os
import random
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.command import Hit, Matcher, Provider
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

try:
    import pyperclip
except Exception:  # noqa: BLE001 — optional convenience, not load-bearing
    pyperclip = None
from textual.widgets.option_list import Option

from replicanta import (
    activity,
    camera,
    extensions,
    fileutil,
    groupchat,
    listen,
    llmclient,
    mud,
    nursery,
    speech,
    tui_commands,
    tui_views,
    voice,
)
from replicanta.organism import Organism
from replicanta.tui_views import (
    STYLE_DIM,
    STYLE_DREAM,
    STYLE_LEARNED,
    STYLE_ORG,
    STYLE_SELF,
    STYLE_USER,
    STYLE_WARN,
)

logger = logging.getLogger(__name__)


class SlashCommands(Provider):
    """Feeds the command palette (ctrl+p) with the slash commands; chosen
    entries fill the chat line and run it."""

    def _run(self, usage):
        """No-arg commands run immediately; commands with placeholder args
        (/chaos 0..1, /swap name…) only fill the chat line — submitting
        the placeholder literally would error or do nonsense."""
        self.app.chat_input.value = usage
        self.app.chat_input.focus()
        if " " not in usage:
            self.app.chat_input.action_submit()

    def _hit(self, name, usage, description, score=1.0, display=None):
        return Hit(
            score=score,
            match_display=display or f"{name}  {description}",
            command=lambda u=usage: self._run(u),
            help=description,
        )

    async def discover(self):
        """Yield every slash command as an unfiltered command-palette hit."""
        for name, usage, description, _category in tui_commands.COMMANDS:
            yield self._hit(name, usage, description)

    async def search(self, query):
        """Yield slash commands whose name/description match the palette query."""
        matcher = Matcher(query)
        for name, usage, description, _category in tui_commands.COMMANDS:
            match = matcher.match(f"{name} {description}")
            if match is not None:
                yield self._hit(
                    name, usage, description, score=match.score, display=match.highlight
                )


class CommandHints(Static):
    """Renders filtered slash-command hints above the chat input."""

    def update_for(self, value):
        if not value.startswith("/"):
            self.update("")
            self.styles.display = "none"
            return
        parts = value.split(None, 1)
        query = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        items = tui_commands.filter_commands(query)
        if prefix:
            items = [c for c in items if c[0] == query]
            if items:
                self.update(f"Usage: {items[0][1]}")
                self.styles.display = "block"
                return
        lines = [f"{usage:<16} {desc}" for _name, usage, desc, _category in items[:8]]
        self.update("\n".join(lines))
        self.styles.display = "block" if lines else "none"


class TabBar(Horizontal):
    """Clickable tab bar that mirrors the TabbedContent panes."""

    TABS: ClassVar[list[tuple[str, str]]] = [
        ("Chat", "chat-pane"),
        ("Mind", "mind-pane"),
        ("Memory", "memory-pane"),
        ("Inner", "inner-pane"),
        ("Cells", "cells-pane"),
        ("MUD", "mud-pane"),
    ]

    def compose(self) -> ComposeResult:
        for label, pane in self.TABS:
            yield Button(label, id=f"tab-{pane}")

    def set_active(self, pane):
        for button in self.query(Button):
            button.variant = "primary" if button.id == f"tab-{pane}" else "default"


class ActivityLabel(Static):
    """Compact, transient activity indicator in the status bar."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_text = None
        self._visible = True

    def show(self, text):
        self.styles.display = "block"
        self._visible = True
        if text == self._last_text:
            return
        self._last_text = text
        self.update(text)

    def clear(self):
        if not self._visible and self._last_text == "":
            return
        self._last_text = ""
        self._visible = False
        self.update("")
        self.styles.display = "none"


class MutationBanner(Horizontal):
    """Sticky banner for pending extension patches with approve/reject/why."""

    def compose(self) -> ComposeResult:
        yield Static("", id="mutation-summary")
        yield Button("Approve", id="mutation-approve", variant="success")
        yield Button("Reject", id="mutation-reject", variant="error")
        yield Button("Why?", id="mutation-why")


class QuickActions(Grid):
    """One-click sidebar buttons for common state changes."""

    def compose(self) -> ComposeResult:
        yield Button("Sleep / Wake", id="qa-sleep")
        yield Button("Voice", id="qa-voice")
        yield Button("Listen", id="qa-listen")
        yield Button("Look", id="qa-look")
        yield Button("MUD", id="qa-mud")


class Toast(Static):
    """Transient bottom-of-screen feedback for errors and warnings."""

    def show(self, message, duration=3.0):
        self.update(message)
        self.styles.display = "block"
        self.set_timer(duration, lambda: setattr(self.styles, "display", "none"))


class HelpScreen(ModalScreen):
    """Overlay with every slash command and key binding."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close")]

    def compose(self) -> ComposeResult:
        """Build the help overlay from the generated slash-command text."""
        yield Static(tui_commands.help_text(), id="help")


class CommandPalette(Screen):
    """Searchable slash-command palette."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close")]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type a command…", id="palette-input")
        yield ListView(id="palette-results")

    def on_show(self):
        self.query_one("#palette-input", Input).focus()
        self._render("")

    def on_input_changed(self, event):
        self._render(event.value)

    def _render(self, query):
        results = self.query_one("#palette-results", ListView)
        results.clear()
        items = tui_commands.filter_commands(query)
        if not items:
            results.append(ListItem(Static("No matches")))
            return
        categories = {}
        for name, usage, desc, category in items:
            categories.setdefault(category, []).append((name, usage, desc))
        for category in ("State", "Voice", "Senses", "MUD", "Organisms", "System", "Help"):
            if category not in categories:
                continue
            header = ListItem(Static(f"[dim]{category}[/dim]"))
            header.disabled = True
            results.append(header)
            for name, usage, desc in categories[category]:
                item = ListItem(
                    Vertical(
                        Static(f"[bold]{usage}[/bold]", classes="palette-usage"),
                        Static(f"{desc}", classes="palette-desc"),
                    )
                )
                item.data = name
                results.append(item)

    def on_list_view_selected(self, event):
        item = event.item
        command = getattr(item, "data", None)
        if command:
            self.dismiss(command)
        else:
            self.dismiss(None)


class OrganismMenuScreen(ModalScreen):
    """Left-click dropdown on a sidebar organism: swap / rename / cancel.
    Dismisses with (action, name) or None."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close")]

    def __init__(self, name, is_current):
        """Create a context menu for the named organism."""
        super().__init__()
        self._name = name
        self._is_current = is_current

    def compose(self) -> ComposeResult:
        """Build swap / rename / move-to-group / cancel options."""
        options = []
        if not self._is_current:
            options.append(Option(f"swap to {self._name}", id="swap"))
        options.append(Option(f"rename {self._name}", id="rename"))
        options.append(Option(f"move {self._name} to group…", id="group"))
        options.append(Option("cancel", id="cancel"))
        yield OptionList(*options, id="org-menu")

    def on_mount(self):
        """Focus the option list so Enter works immediately."""
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event):
        """Dismiss with (action, name) or None on cancel."""
        action = event.option.id
        self.dismiss(None if action == "cancel" else (action, self._name))


class NamePromptScreen(ModalScreen):
    """Generic one-line name prompt (groups, renames). Dismisses with the
    typed name (stripped) or None on escape."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close")]
    INPUT_ID: ClassVar[str] = "name-input"

    def __init__(self, placeholder):
        """Create a one-line input prompt with the given placeholder."""
        super().__init__()
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        """Build the single input field."""
        yield Input(placeholder=self._placeholder, id=self.INPUT_ID)

    def on_mount(self):
        """Focus the input field on open."""
        self.query_one(Input).focus()

    def on_input_submitted(self, event):
        """Dismiss with the stripped value, or None if empty."""
        self.dismiss(event.value.strip() or None)


class RenameScreen(NamePromptScreen):
    """Prompt for a new organism name."""

    INPUT_ID: ClassVar[str] = "rename-input"

    def __init__(self, name):
        """Create a rename prompt for the named organism."""
        super().__init__(f"new name for {name} (letters, digits, - and _)")


class GroupMenuScreen(ModalScreen):
    """Left-click dropdown on a sidebar group header: rename / remove /
    cancel. Dismisses with (action, group_name) or None."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close")]

    def __init__(self, name):
        """Create a context menu for the named group header."""
        super().__init__()
        self._name = name

    def compose(self) -> ComposeResult:
        """Build rename / remove / cancel options."""
        yield OptionList(
            Option(f"rename {self._name}", id="rename"),
            Option(f"remove group {self._name} (organisms stay)", id="remove"),
            Option("cancel", id="cancel"),
            id="group-menu",
        )

    def on_mount(self):
        """Focus the option list so Enter works immediately."""
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event):
        """Dismiss with (action, group_name) or None on cancel."""
        action = event.option.id
        self.dismiss(None if action == "cancel" else (action, self._name))


class GroupPickScreen(ModalScreen):
    """Pick a group to move an organism into. Dismisses with the group
    name, "" for no group, "new" to create one first, or None on cancel."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close")]

    def __init__(self, org_name, groups):
        """Create a picker for moving org_name into one of the listed groups."""
        super().__init__()
        self._org_name = org_name
        self._groups = list(groups)

    def compose(self) -> ComposeResult:
        """Build the group list plus no-group / new-group / cancel options."""
        options = [Option(g, id=f"g:{g}") for g in self._groups]
        options.append(Option("(no group)", id="none"))
        options.append(Option("new group…", id="new"))
        options.append(Option("cancel", id="cancel"))
        yield OptionList(*options, id="group-pick")

    def on_mount(self):
        """Focus the option list so Enter works immediately."""
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event):
        """Dismiss with the chosen group name, '' for none, 'new', or None."""
        oid = event.option.id
        if oid == "cancel":
            self.dismiss(None)
        elif oid == "none":
            self.dismiss("")
        elif oid == "new":
            self.dismiss("new")
        else:
            self.dismiss(oid[2:])


class CellDetailScreen(ModalScreen):
    """Inspector for one neural-memory cell: what kind of object it is
    plus the metadata it carries. Escape or click anywhere to close."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close")]

    def __init__(self, cell):
        """Create an inspector for a single neural-memory cell."""
        super().__init__()
        self._cell = cell

    def compose(self) -> ComposeResult:
        """Build the cell detail read-out."""
        yield Static(Text(tui_views.cell_detail_text(self._cell)), id="cell-detail")

    def on_click(self, event):
        """Dismiss on any mouse click (including outside the panel)."""
        self.dismiss()


class ModulesScreen(ModalScreen):
    """Enable or disable discovered Lua modules and reload the organism's
    module set. Space/enter toggles, s saves & reloads, esc closes."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "close"),
        Binding("s", "save", "save & reload"),
    ]

    def __init__(self, loader):
        """Create the module manager for the given ModuleLoader."""
        super().__init__()
        self._loader = loader
        self._enabled = set()
        enabled = loader.modules_config.get("enabled")
        if enabled is not None:
            self._enabled = set(enabled)
        elif loader.modules:
            # No config yet; treat currently loaded modules as the baseline.
            self._enabled = set(loader.modules)

    def compose(self) -> ComposeResult:
        """Build the title, module list, and detail label."""
        yield Label(
            "Modules — space/enter toggles · s saves & reloads · esc closes",
            id="modules-title",
        )
        yield ListView(id="module-list")
        yield Label("", id="module-detail")

    def on_mount(self):
        """Populate the list once the DOM is ready."""
        self._refresh_list()

    def _discovered(self):
        """Return discovered manifests sorted by name."""
        return sorted(self._loader._discover(), key=lambda m: m.get("name", ""))

    def _refresh_list(self):
        list_view = self.query_one("#module-list", ListView)
        list_view.clear()
        for manifest in self._discovered():
            name = manifest.get("name", "")
            marker = "[x]" if name in self._enabled else "[ ]"
            desc = manifest.get("description", "")
            text = f"{marker} {name}" + (f" — {desc}" if desc else "")
            list_view.append(ListItem(Label(text), id=f"mod-{name}"))
        if list_view.children:
            list_view.focus()

    def on_list_view_selected(self, event):
        """Toggle the selected module and refresh its detail label."""
        item = event.item
        if item.id is None:
            return
        name = item.id.split("-", 1)[1]
        if name in self._enabled:
            self._enabled.discard(name)
        else:
            self._enabled.add(name)
        label = item.query_one(Label)
        manifest = next(
            (m for m in self._discovered() if m.get("name") == name), {}
        )
        marker = "[x]" if name in self._enabled else "[ ]"
        desc = manifest.get("description", "")
        text = f"{marker} {name}" + (f" — {desc}" if desc else "")
        label.update(text)
        self._show_detail(name)

    def _show_detail(self, name):
        manifest = next(
            (m for m in self._discovered() if m.get("name") == name), {}
        )
        lines = [f"{name} v{manifest.get('version', '?')}"]
        deps = manifest.get("depends")
        if deps:
            lines.append(f"depends: {', '.join(deps)}")
        provides = manifest.get("provides")
        if provides:
            lines.append(f"provides: {', '.join(provides)}")
        description = manifest.get("description")
        if description:
            lines.append(description)
        self.query_one("#module-detail", Label).update("\n".join(lines))

    def action_save(self):
        """Persist the enabled set, reload modules, and dismiss the screen."""
        from replicanta import config as project_config

        cfg = project_config.load_config(self._loader.root)
        cfg.setdefault("modules", {}).update(self._loader.modules_config)
        cfg["modules"]["enabled"] = sorted(self._enabled)
        project_config.save_config(self._loader.root, cfg)
        self._loader.load_all()
        self.dismiss(True)


# role -> log style (Rich markup); engine events get their own styles
# (STYLE_USER, STYLE_ORG, STYLE_DIM, STYLE_DREAM, STYLE_LEARNED, STYLE_SELF and
# STYLE_WARN are imported from tui_views.py so view styling lives in one place.)

NARRATE_INTERVAL = 45.0  # seconds between self-narrations (each = 5 LLM calls)
VOICE_PROBE_INTERVAL = 60.0  # seconds between ollama reachability probes
ASK_USER_ODDS = 0.35  # chance an idle wake utterance asks the user instead
MUD_TURN_DELAY = 4.0  # seconds between dungeon moves


class OrganismApp(App):
    """Replicanta's terminal front-end, conversation-first: a workspace
    chrome with a top organism bar, a nursery sidebar, a bottom status
    line, and a tabbed main area (chat, mind, memory, inner). Commands:
    /chaos N, /focus X, /sleep, /wake, /revive, /stats, /save, /think,
    /new, /swap, /organisms, /reload, /lua file.lua, /listen,
    /microphone, /look, /camera, /help (or ctrl+p / F1)."""

    TITLE = "Replicanta"

    COMMANDS: ClassVar[set] = App.COMMANDS | {SlashCommands}
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+p", "command_palette", "command palette"),
        Binding("f1", "help", "help"),
        Binding("f2", "show_tab('chat-pane')", "chat"),
        Binding("f3", "show_tab('mind-pane')", "mind"),
        Binding("f4", "show_tab('memory-pane')", "memory"),
        Binding("ctrl+s", "save_now", "save"),
        Binding("ctrl+t", "think_now", "think"),
        Binding("f5", "talk", "talk (push-to-talk)"),
        Binding("f6", "look", "look through the camera"),
        Binding("f7", "show_tab('inner-pane')", "inner"),
        Binding("f8", "show_tab('cells-pane')", "cells"),
        Binding("f9", "modules", "modules"),
        Binding("ctrl+q", "quit", "quit"),
        Binding("f10", "quit", "quit (ctrl+q can be eaten by terminal flow control)"),
        Binding("ctrl+c", "quit_or_hint", "quit (double-tap)"),
        Binding("ctrl+shift+c", "copy_chat", "copy chat log"),
        Binding("ctrl+m", "toggle_mouse", "toggle mouse capture"),
    ]

    CSS = """
    #topbar { height: 1; padding: 0 1; background: $surface; color: $text; }
    #main { height: 1fr; }
    #sidebar { width: 24; background: $surface; color: $text;
               border-right: solid $primary; }
    #sidebar-header { height: 1; padding: 0 1; background: $surface;
                      color: $text-muted; text-style: bold; }
    #quick-actions { height: auto; padding: 1 0; grid-size: 2; grid-gutter: 0 1; }
    #quick-actions > Button { width: 1fr; margin: 0; }
    #sidebar-list { padding: 0; height: 1fr; border: none;
                     background: $surface; }
    #sidebar-list > ListItem { padding: 0 1; }
    #sidebar-list > ListItem.--highlight { background: $primary;
                                            color: $text; }
    #sidebar-list > ListItem.group-header { color: $text-muted;
                                             text-style: bold; }
    #sidebar.-dragging #sidebar-list > ListItem.group-header {
        background: $boost; color: $text; text-style: bold underline; }
    #content { width: 1fr; height: 1fr; }
    #tab-bar { height: 3; padding: 0 1; }
    #tab-bar > Button { min-width: 8; margin: 0 1; }
    TabbedContent { height: 1fr; }
    TabbedContent > ContentTabs { display: none; }
    #dreams { height: 1fr; padding: 0 1; }
    #pending { height: auto; max-height: 4; padding: 0 1; color: $success; }
    #mind, #memory, #inner { padding: 1 2; }
    #inner { overflow-y: auto; }
    #command-hints { height: auto; max-height: 4; padding: 0 1;
                      color: $text-muted; }
    #mutation-banner { height: auto; display: none; padding: 0 1;
                       background: $warning-darken-2; color: $text; }
    #mutation-banner > Static { width: 1fr; content-align: left middle; }
    #mutation-banner > Button { min-width: 8; margin: 0 1; }
    #chat { height: 3; border: solid yellow; }
    #toast { height: auto; display: none; padding: 0 1;
             background: $error-darken-2; color: $text; }
    #help { border: round green; padding: 1 2; width: 60; height: auto; }
    OrganismMenuScreen, RenameScreen, NamePromptScreen, GroupMenuScreen,
    GroupPickScreen { align: center middle; }
    #org-menu, #group-menu, #group-pick { border: round $primary; width: 44;
                height: auto; background: $surface; }
    #rename-input, #name-input { border: round $primary; width: 60; }
    CellDetailScreen { align: center middle; }
    #cell-detail { border: round $secondary; padding: 1 2; width: 64;
                  height: auto; background: $surface; }
    #bottombar { height: 1; padding: 0 1; background: $surface;
                  color: $text-muted; }
    #activity { width: auto; }
    #bottombar-text { width: 1fr; content-align: right middle; }
    CommandPalette { align: center middle; }
    #palette { width: 60; height: auto; max-height: 24; border: round $primary;
               background: $surface; padding: 1 2; }
    #palette-results { height: auto; max-height: 18; border: none;
                       background: $surface; }
    #palette-results > ListItem { padding: 0 1; }
    #palette-results > ListItem.--highlight { background: $primary; color: $text; }
    .palette-usage { text-style: bold; }
    .palette-desc { color: $text-muted; }
    """

    def __init__(self, organism, root=None, spawn=None):
        """Wire the organism, nursery, voice, camera, and MUD state for the TUI.

        Args:
            organism: The currently active Organism instance.
            root: Nursery root directory (defaults to two levels above the
                organism's directory).
            spawn: Keyword arguments passed to Organism() when birthing or
                swapping organisms (wake/sleep/chaos overrides).
        """
        super().__init__()
        self.org = organism
        # nursery root for /new, /swap, /organisms; when the organism was
        # born in a nursery (organisms/<name>/), the root is two levels up
        self.root = Path(root) if root is not None else organism.dir_path.parent.parent
        # kwargs for Organism() when birthing/swapping (wake/sleep/chaos)
        self._spawn = dict(spawn or {})
        # metadata grid behind the cells tab, for click-to-inspect
        self._cells_grid = []
        # button of the most recent click anywhere; lets the sidebar tell a
        # right-click (context menu) apart from a left-click selection
        self._last_click_button = None
        # sidebar drag-and-drop: (name, x, y) of a left-press on an
        # organism, then the dragged organism's name once it moves
        self._drag_candidate = None
        self._dragging = None
        # active group chat (GroupChat) and its reply worker flag
        self._group = None
        self._group_responding = False
        self.chat_input = None
        self._narrating = False
        self._responding = False
        self._completion_index = 0
        self._chat_history = []
        self._history_index = -1
        self._history_draft = ""
        self._suppress_changed = False
        self._probing_voice = False
        self._voice_announced = None
        self._self_talk_on = False
        self._self_talking = False
        self._rng = random.Random()  # nosec B311 - UI variety RNG, not cryptography
        self._last_was_question = False
        self._pending_text = ""
        self._pending_visible = False
        self._busy_frame = 0
        self._typing_timer = None
        self._typing_last = 0.0
        self.listener = listen.Listener()
        self.camera = camera.Camera()
        self._mud_game = None
        self._mud_hint = None  # one-shot user nudge for the next move
        self._mud_paused = True  # start paused; the human plays the MUD
        self._mud_thinking = False  # a move-choice worker is in flight
        self._mud_turn_gen = 0  # bumped by user moves/hints: stales in-flight
        self._quit_hint_time = 0.0
        self._mind_text = ""
        self._memory_text = ""
        self._topbar_text = ""
        self._bottombar_text = ""
        self._rendered_topbar_text = None
        self._rendered_bottombar_text = None

    def compose(self) -> ComposeResult:
        """Build the top bar, sidebar, tabbed content area, chat input, and bottom bar."""
        yield Static("", id="topbar")
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Static("nursery", id="sidebar-header")
                yield ListView(id="sidebar-list")
                yield QuickActions(id="quick-actions")
            with Vertical(id="content"):
                yield TabBar(id="tab-bar")
                with TabbedContent(initial="chat-pane"):
                    with TabPane("chat", id="chat-pane"):
                        dreams = RichLog(
                            id="dreams",
                            max_lines=1000,
                            wrap=True,
                            markup=True,
                            highlight=False,
                        )
                        dreams.can_focus = False
                        yield dreams
                        yield Static("", id="pending", markup=False)
                    with TabPane("mind", id="mind-pane"), VerticalScroll():
                        yield Static("", id="mind", markup=False)
                    with TabPane("memory", id="memory-pane"), VerticalScroll():
                        yield Static("", id="memory", markup=False)
                    with TabPane("inner", id="inner-pane"), VerticalScroll():
                        yield Static("", id="inner", markup=False)
                    with TabPane("cells", id="cells-pane"), VerticalScroll():
                        yield Static("", id="cells", markup=False)
                    with TabPane("mud", id="mud-pane"), VerticalScroll():
                        yield Static(
                            "MUD output appears here. Type moves in the chat bar.",
                            id="mud",
                            markup=False,
                        )
        yield CommandHints("", id="command-hints")
        yield MutationBanner(id="mutation-banner")
        self.chat_input = Input(
            placeholder="talk to me, or /help …  (tab completes · "
            "F2 chat · F3 mind · F4 memory · F7 inner · F8 cells · F9 modules)",
            id="chat",
        )
        yield self.chat_input
        yield Toast("", id="toast")
        with Horizontal(id="bottombar"):
            yield ActivityLabel("", id="activity")
            yield Static("", id="bottombar-text")

    def on_mount(self):
        """Render the initial organism state and start background timers."""
        self._show_org()
        tab_bar = self._safe_query("#tab-bar", TabBar)
        if tab_bar is not None:
            tab_bar.set_active("chat-pane")
        self.set_interval(1.0, self._on_tick)
        self.set_interval(NARRATE_INTERVAL, self._maybe_narrate)
        self.set_interval(VOICE_PROBE_INTERVAL, self._probe_voice)
        self._probe_voice()
        self._maybe_narrate()
        # start with the cursor in the chat line, not the scrollable log
        self.chat_input.focus()

    def _show_org(self):
        """(Re)render everything that reflects the current organism: chat
        history, status bar, mind/memory tabs. Used on mount and after a
        swap."""
        self._chat_history = [
            line for role, line in self.org.store.chat_log if role == "user"
        ]
        if not self.org.store.chat_log and self.org.store.cycle == 0:
            self._append_log(
                "a tiny replicanta wakes up inside your machine.", STYLE_DIM
            )
            self._append_log(
                "talk to it — it learns from you. /help (or F1) for commands.",
                STYLE_DIM,
            )
        for role, line in self.org.store.chat_log[-100:]:
            self._log_chat(role, line, stamp=False)
        self.org.hooks.emit = lambda msg: self._append_log(
            f"lua · {msg}", STYLE_DIM, stamp=True
        )
        self.refresh_top_bar()
        self._refresh_sidebar()
        self.refresh_status()
        self._refresh_views()

    def _swap_to(self, name, flush=True):
        """Persist the current organism and wake another one in its place.
        In-flight voice workers belong to the old organism: they finish
        against it and their deliveries are dropped by the org-identity
        checks, so swapping is always safe. flush=False is for callers
        that already persisted the organism (e.g. just renamed it)."""
        if flush:
            self.org.flush(force=True)
        old_org = self.org
        nursery.set_current(self.root, name)
        org = Organism(nursery.organism_dir(self.root, name), **self._spawn)
        org.load()
        self.org = org
        if old_org is not None:
            old_org.close()
        # stale workers reset these in their finally blocks anyway; clear
        # them so the new organism is never blocked by the old one's debate
        self._narrating = self._responding = self._self_talking = False
        self.query_one("#dreams", RichLog).clear()
        self._pending_hide()
        self._show_org()
        self._append_log(f"— now living with {name} —", STYLE_DIM, stamp=True)

    # -- actions ---------------------------------------------------------
    def action_show_tab(self, pane):
        self.query_one(TabbedContent).active = pane
        tab_bar = self._safe_query("#tab-bar", TabBar)
        if tab_bar is not None:
            tab_bar.set_active(pane)
        # keep typing in the chat line, never stranded by a pane switch
        if self.chat_input is not None:
            self.chat_input.focus()

    def on_button_pressed(self, event):
        """Route tab-bar, mutation, and quick-action button presses."""
        button_id = event.button.id
        if button_id and button_id.startswith("tab-"):
            self.action_show_tab(button_id[4:])
            return
        if button_id == "mutation-approve":
            entry = extensions.approve(self.org.extension_path)
            if entry:
                self.org.store.remember("skill", f"patch applied ({entry['kind']})")
                self._append_log(
                    f"patch applied ({entry['kind']}) — live now, no restart needed",
                    STYLE_LEARNED,
                    stamp=True,
                )
            self._update_mutation_banner()
            return
        if button_id == "mutation-reject":
            entry = extensions.reject(self.org.extension_path)
            if entry:
                self.org.store.remember("skill", f"patch rejected ({entry['kind']})")
                self._append_log(
                    f"patch rejected ({entry['kind']})", STYLE_DIM, stamp=True
                )
            self._update_mutation_banner()
            return
        if button_id == "mutation-why":
            self._show_mutation_why()
            return
        mapping = {
            "qa-sleep": self.action_sleep_wake,
            "qa-voice": self.action_voice,
            "qa-listen": self.action_talk,
            "qa-look": self.action_look,
            "qa-mud": self.action_mud,
        }
        action = mapping.get(button_id)
        if action:
            action()

    def action_help(self):
        self.push_screen(HelpScreen())

    def action_command_palette(self):
        """Open the searchable slash-command palette and fill the chat line."""

        def _fill(command):
            if command:
                self.chat_input.value = f"{command} "
                self.chat_input.focus()

        self.push_screen(CommandPalette(), callback=_fill)

    def action_modules(self):
        loader = getattr(self.org, "module_loader", None)
        if loader is None:
            self._append_log("module loader unavailable", STYLE_WARN)
            return
        self.push_screen(ModulesScreen(loader))

    def action_save_now(self):
        self.org.flush(force=True)
        if self._group is not None:
            for org in self._group.members.values():
                if org is not self.org:
                    org.flush(force=True)

    def action_quit(self):
        """Quit cleanly: persist organism state, tear down the UI, then
        make sure the process actually dies. Thread workers blocked in an
        ollama call cannot be cancelled, and the default executor joins
        them at interpreter exit — without the hard-exit fallback the UI
        closes but the process hangs until the call times out."""
        self.action_save_now()
        if self.org is not None:
            self.org.close()
        self._arm_hard_exit()
        self.exit()

    def action_copy_chat(self):
        """Copy the visible chat log to the system clipboard. Falls back to
        writing plain text to a temp file if no clipboard backend is available."""
        lines = []
        for role, text in self.org.store.chat_log[-250:]:
            who = "you" if role == "user" else self._org_name()
            lines.append(f"{who}: {text}")
        body = "\n".join(lines)
        if pyperclip is not None:
            try:
                pyperclip.copy(body)
                self._append_log("— chat log copied to clipboard —", STYLE_DIM, stamp=True)
                return
            except Exception:  # noqa: BLE001,S110 # nosec — clipboard may fail in ssh/tmux; fall through to file
                pass
        path = Path(tempfile.gettempdir()) / f"replicanta-chat-{int(time.time())}.txt"
        path.write_text(body, encoding="utf-8")
        self._append_log(f"— chat log saved to {path} —", STYLE_DIM, stamp=True)

    def _export_chat(self, path=None):
        """Write the full chat log to a markdown file. Returns the path."""
        from datetime import datetime

        org_name = self._org_name()
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        if path:
            dest = Path(path).expanduser()
        else:
            dest = Path.home() / f"replicanta-chat-{org_name}-{timestamp}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            f"# Chat with {org_name}",
            "",
            f"Exported: {datetime.now(UTC).isoformat()}",
            f"Organism: {org_name}",
            f"Cycles: {self.org.store.cycle}",
            "",
        ]
        for role, text in self.org.store.chat_log:
            who = "You" if role == "user" else org_name
            lines.append(f"## {who}")
            lines.append("")
            lines.append(text)
            lines.append("")

        fileutil.atomic_write_text(dest, "\n".join(lines))
        return dest

    def action_toggle_mouse(self):
        """Toggle Textual's mouse capture. When captured, clicks work inside
        the app but the terminal cannot select text. When released, native
        terminal mouse selection (and copying) works."""
        enabled = not self.mouse_captured
        self.capture_mouse(enabled)
        status = "enabled" if enabled else "disabled"
        self._append_log(f"— mouse capture {status} (ctrl+m to toggle) —", STYLE_DIM, stamp=True)

    def _arm_hard_exit(self, delay=0.75):
        """Force os._exit(0) after a grace period if the interpreter is
        still alive (stuck joining LLM worker threads). The grace only
        needs to cover Textual's terminal restore (~0.1s); state is
        already flushed by the caller, and file writes are atomic, so
        dying mid-flight cannot corrupt the organism."""

        def killer():
            time.sleep(delay)
            os._exit(0)

        threading.Thread(target=killer, daemon=True).start()

    def action_quit_or_hint(self):
        """Quit on double-tap; show a hint on first ctrl+c press."""
        now = time.monotonic()
        if now - self._quit_hint_time < 2.0:
            self.action_quit()
            return
        self._quit_hint_time = now
        self.notify("press ctrl+c again to quit", timeout=1.0)

    def refresh_top_bar(self):
        """Render the custom top bar: app wordmark, organism identity,
        mood/mental state, and voice/mic/clock indicators."""
        lc = self.org.lifecycle
        icon = {"wake": "🧠", "sleep": "💤", "dead": "🪦"}.get(lc.state, "🧠")
        word = {"wake": "awake", "sleep": "asleep", "dead": "faded"}.get(
            lc.state, lc.state
        )
        mood = self.org.store.belief_value("self", "mood", "calm")
        s = self.org.store
        mental = f"a/r/i {s.arousal:.2f}/{s.rationality:.2f}/{s.irrationality:.2f}"
        mic = " 🎙" if self.listener.recording else ""
        spoken = " 🔊" if speech.enabled else ""
        voice = llmclient.voice_status()
        clock = self.org.probe.clock_utc()
        text = (
            f"Replicanta  │  {icon} {self._org_name()} · {word} · {mood} · "
            f"{mental}  │  {voice}{mic}{spoken}  {clock}"
        )
        self._topbar_text = text
        if text == self._rendered_topbar_text:
            return
        self._rendered_topbar_text = text
        topbar = self._safe_query("#topbar", Static)
        if topbar is not None:
            topbar.update(text)

    def _refresh_sidebar(self):
        """Rebuild the nursery sidebar, highlighting the current organism."""
        lv = self._safe_query("#sidebar-list", ListView)
        if not isinstance(lv, ListView):
            return
        lv.clear()
        current = self.org.dir_path.name
        names = nursery.list_organisms(self.root)
        if not names:
            lv.append(ListItem(Label("(no organisms)")))
            return
        groups = nursery.load_groups(self.root)
        grouped = {m for members in groups.values() for m in members}
        for name in names:
            if name in grouped:
                continue
            marker = "● " if name == current else "  "
            lv.append(ListItem(Label(f"{marker}{name}"), name=name))
        for gname in sorted(groups):
            lv.append(
                ListItem(
                    Label(f"▾ {gname}"), name=f"group:{gname}", classes="group-header"
                )
            )
            for member in groups[gname]:
                marker = "● " if member == current else "  "
                lv.append(ListItem(Label(f"  {marker}{member}"), name=member))

    def on_list_view_selected(self, event):
        """Sidebar selection opens a dropdown (left click or Enter): an
        organism gets swap / rename / move-to-group, a group header gets
        rename / remove."""
        if not event.item.name:
            return
        # a right-click opens the context menu (on_mouse_down); the ListView
        # still posts Selected for it, which must not open a second menu
        if self._last_click_button == 3:
            self._last_click_button = None
            return
        if event.item.name.startswith("group:"):
            self._open_group_menu(event.item.name[6:])
        else:
            self._open_org_menu(event.item.name)

    def _sidebar_item_at(self, screen_x, screen_y):
        """(item, in_sidebar) for a screen position: the sidebar ListItem
        under it (None over chrome/empty space); in_sidebar is False when
        the position is outside the sidebar entirely."""
        widget, _region = self.screen.get_widget_at(screen_x, screen_y)
        node = widget
        while node is not None:
            if isinstance(node, ListItem) and node.name:
                return node, True
            if getattr(node, "id", None) == "sidebar":
                return None, True
            node = node.parent
        return None, False

    def on_mouse_down(self, event):
        """Left button over a sidebar organism may start a drag into a
        group (a plain click never moves far enough to become one).
        Right-click in the sidebar: a group header opens its rename
        prompt, an organism its action menu, empty space the new-group
        prompt. (Textual's Click message is left-button only, so button 3
        is handled directly here.)"""
        if event.button == 1:
            item, _in_sidebar = self._sidebar_item_at(event.screen_x, event.screen_y)
            if item is not None and not item.name.startswith("group:"):
                self._drag_candidate = (item.name, event.screen_x, event.screen_y)
            return
        if event.button != 3:
            return
        item, in_sidebar = self._sidebar_item_at(event.screen_x, event.screen_y)
        if not in_sidebar:
            return
        if item is None:
            self._prompt_new_group()
        elif item.name.startswith("group:"):
            self._prompt_rename_group(item.name[6:])
        else:
            self._open_org_menu(item.name)

    def on_mouse_move(self, event):
        """A left-press that travels far enough becomes a drag: group
        headers light up as drop targets."""
        if self._drag_candidate is None or self._dragging is not None:
            return
        name, x0, y0 = self._drag_candidate
        if abs(event.screen_y - y0) < 1 and abs(event.screen_x - x0) < 3:
            return
        self._drag_candidate = None
        self._dragging = name
        sidebar = self._safe_query("#sidebar", Vertical)
        if sidebar is not None:
            sidebar.set_class(True, "-dragging")
        self._pending_show(f"dragging {name} — drop on a group")

    def on_mouse_up(self, event):
        """Drop: onto a group header or one of its members assigns the
        dragged organism to that group; onto empty sidebar space ungroups
        it; outside the sidebar cancels."""
        self._drag_candidate = None
        if self._dragging is None:
            return
        name, self._dragging = self._dragging, None
        sidebar = self._safe_query("#sidebar", Vertical)
        if sidebar is not None:
            sidebar.set_class(False, "-dragging")
        self._pending_hide()
        item, in_sidebar = self._sidebar_item_at(event.screen_x, event.screen_y)
        if not in_sidebar:
            return
        if item is None:
            target_group = None
        elif item.name == name:
            return
        elif item.name.startswith("group:"):
            target_group = item.name[6:]
        else:
            target_group = nursery.group_of(self.root, item.name)
        if target_group != nursery.group_of(self.root, name):
            self._assign_to_group(name, target_group)

    def on_click(self, event):
        """Left-click a neural-memory cell (F8 grid) to inspect what kind
        of object it holds and its metadata. Grid geometry: one header
        line, then CELLS_ROWS lines of 2-column-wide cells."""
        self._last_click_button = event.button
        if getattr(event.widget, "id", None) != "cells":
            return
        col = event.x // 2
        row = event.y - 1
        if not (0 <= row < tui_views.CELLS_ROWS and 0 <= col < tui_views.CELLS_COLS):
            return
        idx = row * tui_views.CELLS_COLS + col
        if idx < len(self._cells_grid) and self._cells_grid[idx]:
            self.push_screen(CellDetailScreen(self._cells_grid[idx]))

    def _open_org_menu(self, name):
        def on_choice(result):
            if not result:
                return
            action, org_name = result
            if action == "swap" and org_name != self.org.dir_path.name:
                self._swap_to(org_name)
            elif action == "rename":
                self._prompt_rename(org_name)
            elif action == "group":
                self._pick_group_for(org_name)

        self.push_screen(
            OrganismMenuScreen(name, name == self.org.dir_path.name), on_choice
        )

    def _prompt_rename(self, name):
        def on_name(new_name):
            if not new_name or new_name == name:
                return
            try:
                self._rename_org(name, new_name)
            except ValueError as exc:
                self.notify(str(exc), severity="error", timeout=4)

        self.push_screen(RenameScreen(name), on_name)

    def _rename_org(self, old, new):
        """Rename an organism on disk and keep the app pointed at it. The
        awake organism is flushed before its directory moves (otherwise
        the next flush would resurrect the old path), then swapped to the
        new directory; sleeping ones just need a sidebar refresh."""
        if old == self.org.dir_path.name:
            self.org.flush(force=True)
            nursery.rename(self.root, old, new)
            self._swap_to(new, flush=False)
        else:
            nursery.rename(self.root, old, new)
            self._refresh_sidebar()
        self.notify(f"renamed {old} → {new}", timeout=3)

    # -- nursery groups -----------------------------------------------------

    def _open_group_menu(self, gname):
        def on_choice(result):
            if not result:
                return
            action, name = result
            if action == "rename":
                self._prompt_rename_group(name)
            elif action == "remove":
                try:
                    nursery.remove_group(self.root, name)
                except ValueError as exc:
                    self.notify(str(exc), severity="error", timeout=4)
                    return
                self._refresh_sidebar()
                self.notify(f"removed group {name} (organisms ungrouped)", timeout=3)

        self.push_screen(GroupMenuScreen(gname), on_choice)

    def _prompt_new_group(self, on_created=None):
        def on_name(name):
            if not name:
                return
            try:
                nursery.create_group(self.root, name)
            except ValueError as exc:
                self.notify(str(exc), severity="error", timeout=4)
                return
            self._refresh_sidebar()
            self.notify(f"created group {name}", timeout=3)
            if on_created is not None:
                on_created(name)

        self.push_screen(
            NamePromptScreen("new group name (letters, digits, spaces, - _ .)"), on_name
        )

    def _prompt_rename_group(self, gname):
        def on_name(new_name):
            if not new_name or new_name == gname:
                return
            try:
                nursery.rename_group(self.root, gname, new_name)
            except ValueError as exc:
                self.notify(str(exc), severity="error", timeout=4)
                return
            self._refresh_sidebar()
            self.notify(f"renamed group {gname} → {new_name}", timeout=3)

        self.push_screen(NamePromptScreen(f"new name for group {gname}"), on_name)

    def _assign_to_group(self, org_name, group_name):
        try:
            nursery.assign(self.root, org_name, group_name)
        except ValueError as exc:
            self.notify(str(exc), severity="error", timeout=4)
            return
        self._refresh_sidebar()
        self.notify(f"{org_name} → {group_name or 'ungrouped'}", timeout=3)

    def _pick_group_for(self, org_name):
        def on_pick(choice):
            if choice is None:
                return
            if choice == "new":
                self._prompt_new_group(
                    on_created=lambda name: self._assign_to_group(org_name, name)
                )
                return
            self._assign_to_group(org_name, choice or None)

        self.push_screen(
            GroupPickScreen(org_name, nursery.list_groups(self.root)), on_pick
        )

    def action_think_now(self):
        self._maybe_narrate()

    def action_talk(self):
        self._toggle_listen()

    def action_look(self):
        self._look_now()

    def action_sleep_wake(self):
        """Toggle between wake and sleep states."""
        if self.org.lifecycle.state == "wake":
            self.handle_command("/sleep")
        else:
            self.handle_command("/wake")

    def action_voice(self):
        """Toggle spoken voice output."""
        self.handle_command("/voice")

    def action_mud(self):
        """Toggle the MUD mini-game."""
        self._mud_command([])

    # -- sight (camera) ------------------------------------------------------
    def _look_now(self):
        """Grab one camera frame and have a local vision model put it into
        words; the organism remembers the sight and the voice can talk
        about it. All failures land as log lines."""
        self._append_log("the organism opens an eye…", STYLE_DIM)
        self._look_worker()

    @work(thread=True)
    def _look_worker(self):
        frame = self.camera.grab()
        if frame is None:
            self.call_from_thread(
                self._append_log,
                "no camera frame (plugged in? opencv installed? see /camera list)",
                STYLE_WARN,
            )
            self.call_from_thread(self.show_toast, "Camera not found")
            return
        try:
            sight = llmclient.describe_image(frame)
        except Exception as exc:  # noqa: BLE001 — vision model offline etc.
            self.call_from_thread(self._append_log, f"sight failed: {exc}", STYLE_WARN)
            return
        self.call_from_thread(self._set_sight, sight)

    # -- mud mode ------------------------------------------------------------
    def _mud_command(self, args):
        """Dispatch /mud subcommands: bare toggles, the rest control or
        inspect the running game."""
        if not args:
            self._toggle_mud()
            return
        sub = args[0]
        if sub in ("map", "story", "quest"):
            game = self._mud_game
            if game is None:
                self._append_log(
                    f"/mud {sub}: no game running (start with /mud)", STYLE_DIM
                )
                return
            render = {
                "map": mud.render_map,
                "story": mud.render_story,
                "quest": mud.render_quest,
            }[sub]
            self._append_log(render(game), STYLE_DREAM)
        elif sub == "pause":
            if self._mud_game is None:
                self._append_log("/mud pause: no game running.", STYLE_DIM)
            elif self._mud_paused:
                self._append_log("mud: already paused (/mud resume)", STYLE_DIM)
            else:
                self._mud_paused = True
                self._append_log(
                    "— the dungeon holds its breath (paused; /mud resume, "
                    "/mud step, or type a command) —",
                    STYLE_DIM,
                    stamp=True,
                )
                self.refresh_status()
        elif sub == "resume":
            if self._mud_game is None:
                self._append_log("/mud resume: no game running.", STYLE_DIM)
            elif not self._mud_paused:
                self._append_log("mud: not paused.", STYLE_DIM)
            else:
                self._mud_paused = False
                self._append_log("— the dungeon stirs again —", STYLE_DIM, stamp=True)
                self.refresh_status()
                self._mud_next()
        elif sub == "step":
            self._mud_step()
        elif sub == "reset":
            self._mud_reset()
        elif sub == "scenario":
            description = " ".join(args[1:]).strip()
            if not description:
                self._append_log(
                    "/mud scenario needs a description, e.g. "
                    "/mud scenario a haunted space station",
                    STYLE_DIM,
                )
                return
            self._mud_scenario(description)
        else:
            self._append_log(
                "/mud [map|story|quest|pause|resume|step|reset|scenario <description>]",
                STYLE_DIM,
            )

    def _toggle_mud(self):
        """/mud: start or stop the organism's dungeon crawl. The world is
        deterministic; the voice picks the moves (wanderer fallback)."""
        if self._mud_game is not None:
            self._mud_stop()
            return
        self._mud_start()

    def _mud_start(self, scenario=None, fresh=False):
        """Begin a game: with the given scenario, else resuming the saved
        session when one exists, else the default dungeon."""
        session = None
        if scenario is None and not fresh:
            scenario, session = self._mud_restore()
        game = mud.MudGame(scenario)
        if session is not None:
            # the session tracks map/story but not room/inventory: replay
            # the logged commands to bring back the exact game state
            for actor, command, _turn in session.command_log:
                game.act_event(command, actor=actor)
            game.session = session
        self._mud_game = game
        self._mud_paused = True
        if session is not None:
            self._append_log(
                f"— the organism returns to {game.scenario.title} "
                f"(turn {game.turns}) —",
                STYLE_DREAM,
                stamp=True,
            )
        else:
            self._append_log(
                "— the dungeon opens for you; the organism stands beside you "
                "as a companion —",
                STYLE_DREAM,
                stamp=True,
            )
            self._append_log(mud.build_premise(self.org, game.scenario), STYLE_DREAM)
        self._append_log(game.look(), STYLE_DREAM)
        self.org.store.remember("mud", f"started {game.scenario.title}")
        self._mud_save_session(game)
        self.refresh_status()

    def _mud_stop(self):
        """End the current game, persisting its session for a later resume."""
        game, self._mud_game = self._mud_game, None
        self._mud_paused = False
        self._mud_save_session(game)
        self._append_log(
            f"— the dungeon fades (stopped after {game.turns} turns) —",
            STYLE_DIM,
            stamp=True,
        )
        self.org.store.remember(
            "mud", f"left {game.scenario.title} after {game.turns} turns"
        )
        self.refresh_status()

    def _mud_reset(self):
        """Restart the current scenario fresh, discarding the session."""
        game = self._mud_game
        if game is None:
            self._append_log("/mud reset: no game running.", STYLE_DIM)
            return
        scenario = game.scenario
        self._mud_game = None
        self._mud_paused = False
        self._append_log("— the dungeon resets —", STYLE_DIM, stamp=True)
        self._mud_start(scenario=scenario, fresh=True)

    def _mud_step(self):
        """/mud step: exactly one organism turn while paused."""
        if self._mud_game is None:
            self._append_log("/mud step: no game running.", STYLE_DIM)
            return
        if not self._mud_paused:
            self._append_log("auto-turns are running — /mud pause first", STYLE_DIM)
            return
        if self._mud_thinking:
            return  # a move is already being chosen
        self._mud_thinking = True
        self._mud_turn()

    def _mud_scenario(self, description):
        """/mud scenario <description>: stop the current game and dream up
        a new scenario with the voice (off the UI thread)."""
        if self._mud_game is not None:
            self._mud_stop()
        self._append_log(f"dreaming up a scenario: {description}…", STYLE_DIM)
        self._mud_scenario_worker(description)

    @work(thread=True)
    def _mud_scenario_worker(self, description):
        self.call_from_thread(self.set_activity, "MUD dreaming")
        try:
            scenario = mud.generate_scenario(description, self.org)
        except Exception as exc:  # noqa: BLE001 — voice offline etc.
            self.call_from_thread(
                self._append_log, f"/mud scenario failed: {exc}", STYLE_WARN
            )
            return
        finally:
            self.call_from_thread(self.clear_activity)
        self.call_from_thread(self._mud_start_scenario, scenario)

    def _mud_start_scenario(self, scenario):
        self._mud_save_scenario(scenario)
        self._append_log(f"new scenario: {scenario.title}", STYLE_LEARNED, stamp=True)
        self._mud_start(scenario=scenario, fresh=True)

    # -- mud persistence ------------------------------------------------------
    def _mud_artifacts_dir(self):
        return self.org.dir_path / "artifacts"

    def _mud_save_session(self, game=None):
        """Persist the session after every turn and on stop via the
        organism-side BeliefStore."""
        game = game or self._mud_game
        if game is None:
            return
        self.org.store.save_mud_session(game.session)

    def _mud_load_session(self):
        return self.org.store.load_mud_session()

    def _mud_restore(self):
        """(scenario, session) from disk for resume, or (None, None) to
        start fresh: a finished or scenario-less session is not resumed."""
        session = self._mud_load_session()
        if session is None or session.outcome is not None:
            return None, None
        scenario = self._mud_load_scenario(session.scenario_id)
        if scenario is None:
            return None, None
        return scenario, session

    def _mud_load_scenario(self, slug):
        """A saved generated scenario by slug, or the built-in default.
        The slug comes from a resumed session on disk, so re-validate it
        the same way writes produce it before touching the filesystem."""
        if not slug or fileutil.slug(slug) != slug:
            return None
        path = self._mud_artifacts_dir() / "mud" / "scenarios" / f"{slug}.json"
        try:
            if path.exists():
                return mud.validate_scenario(json.loads(path.read_text()))
        except (OSError, ValueError) as exc:
            self._append_log(f"mud: couldn't load scenario {slug} ({exc})", STYLE_WARN)
        default = mud.default_scenario()
        if fileutil.slug(default.title) == slug:
            return default
        return None

    def _mud_save_scenario(self, scenario):
        """Save a generated scenario to artifacts/mud/scenarios/<slug>.json."""
        try:
            directory = self._mud_artifacts_dir() / "mud" / "scenarios"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{fileutil.slug(scenario.title)}.json"
            fileutil.atomic_write_text(
                path, json.dumps(mud.scenario_to_json(scenario), indent=1)
            )
            self._append_log(
                f"scenario saved: artifacts/mud/scenarios/{path.name}", STYLE_DIM
            )
        except OSError as exc:
            self._append_log(f"mud: couldn't save scenario ({exc})", STYLE_WARN)

    # -- mud turns -------------------------------------------------------------
    @work(thread=True)
    def _mud_turn(self):
        game = self._mud_game
        if game is None:
            self._mud_thinking = False
            return
        self.call_from_thread(self.set_activity, "MUD thinking")
        hint, self._mud_hint = self._mud_hint, None
        gen = self._mud_turn_gen
        try:
            command, reason = mud.choose_action(
                game, hint=hint, rng=self._rng, org=self.org
            )
            self.call_from_thread(self._mud_apply, game, command, "organism", gen, reason)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._worker_error, "MUD turn", exc)
        finally:
            self.call_from_thread(self.clear_activity)

    def _mud_apply(self, game, command, actor="organism", gen=None, reason=None):
        if actor == "organism":
            self._mud_thinking = False
        if self._mud_game is not game:
            return  # stopped (or restarted) meanwhile
        if actor == "organism" and gen is not None and gen != self._mud_turn_gen:
            # a user move (or hint) landed while this move was being
            # chosen — it was picked from a world that no longer
            # exists; dropping it keeps the heartbeat honest
            self._append_log(
                "> (the organism hesitates — the moment passed)", STYLE_DIM
            )
            self._mud_schedule()
            return
        if actor == "organism" and reason:
            self._append_log(reason, STYLE_DIM)
        self._append_log(f"> {command}", STYLE_SELF)
        result = game.act_event(command, actor=actor)
        self._append_log(result.text, STYLE_DREAM)
        if result.plot:
            self._append_log(result.plot, STYLE_LEARNED)
        if game.finished:
            outcome = "won" if game.won else "lost"
            self._append_log(
                f"— {game.scenario.title} is {outcome} in {game.turns} turns —",
                STYLE_LEARNED,
                stamp=True,
            )
            self.org.store.remember(
                "mud", f"{outcome} {game.scenario.title} in {game.turns} turns"
            )
            self._mud_save_session(game)
            self._mud_game = None
            self._mud_paused = False
            self.refresh_status()
            return
        self._mud_save_session(game)
        if actor == "organism":
            # the organism's heartbeat: user commands execute instantly and
            # never schedule (a timer is pending, or the game is paused)
            self._mud_schedule()

    def _mud_schedule(self):
        if not self._mud_paused:
            self.set_timer(MUD_TURN_DELAY, self._mud_next)

    def _mud_next(self):
        if (
            self._mud_game is not None
            and not self._mud_paused
            and not self._mud_thinking
        ):
            self._mud_thinking = True
            self._mud_turn()

    def _set_sight(self, sight):
        self.org.see(sight)
        self._append_log(f"the organism sees: {sight}", STYLE_LEARNED, stamp=True)

    def _camera(self, args):
        """Manage the eye: bare = status, list = devices, use <dev> = pick
        one (index, /dev/videoN, or name substring)."""
        if not args:
            self._append_log(
                f"camera: /dev/video{self.camera.device} · vision model "
                f"{llmclient.vision_model()} · /camera list · "
                "/camera use <device>",
                STYLE_DIM,
            )
            return
        if args[0] == "list":
            cams = camera.list_cameras()
            if not cams:
                self._append_log("no cameras found (plug one in)", STYLE_WARN)
                self.show_toast("No cameras found")
            for index, dev_name in cams:
                self._append_log(f"  /dev/video{index}  {dev_name}", STYLE_DIM)
            return
        if args[0] == "use" and len(args) == 2:
            try:
                index, dev_name = self.camera.set_device(args[1])
            except LookupError as exc:
                self._append_log(f"/camera: {exc}", STYLE_WARN)
                self.show_toast(f"Camera error: {exc}")
                return
            self._append_log(f"camera: using /dev/video{index} ({dev_name})", STYLE_DIM)
            return
        self._append_log("/camera [list|use <device>]", STYLE_DIM)

    # -- heard voice (push-to-talk) -----------------------------------------
    def _toggle_listen(self):
        """Push-to-talk: first F5//listen starts the mic, second stops it
        and transcribes; the text goes to the organism like a typed line."""
        if self.listener.recording:
            audio = self.listener.stop()
            self._append_log("transcribing…", STYLE_DIM)
            self._transcribe_then_say(audio)
        else:
            self.listener.start()
            if self.listener.recording:
                self._append_log("listening… (F5 or /listen again to stop)", STYLE_DIM)
            else:
                self._append_log("no microphone (device missing or busy?)", STYLE_WARN)
        self.refresh_status()

    @work(thread=True)
    def _transcribe_then_say(self, audio):
        text = self.listener.transcribe(audio)
        if text:
            self.call_from_thread(self.handle_chat, text)
        else:
            self.call_from_thread(self._append_log, "(heard nothing)", STYLE_DIM)
        self.call_from_thread(self.refresh_status)

    def _microphone(self, args):
        """Manage the heard voice: bare = status, list = input devices,
        use <id|name> = choose the device for future recordings."""
        if not args:
            mic = self.listener.mic_spec or "default"
            stt_model, stt_device, stt_compute = listen._stt_config()
            self._append_log(
                f"microphone: {mic} · stt {stt_model} "
                f"({stt_device}/{stt_compute}) · "
                "/microphone list · /microphone use <device>",
                STYLE_DIM,
            )
            return
        if args[0] == "list":
            mics = listen.list_microphones()
            if not mics:
                self._append_log("no input devices found", STYLE_WARN)
                self.show_toast("No microphone matched")
            for dev_id, dev_name in mics:
                self._append_log(f"  {dev_name}  [{dev_id}]", STYLE_DIM)
            return
        if args[0] == "use" and len(args) == 2:
            try:
                matched = self.listener.set_mic(args[1])
            except (LookupError, OSError) as exc:
                self._append_log(f"/microphone: {exc}", STYLE_WARN)
                self.show_toast(f"Microphone error: {exc}")
                return
            self._append_log(f"microphone: using {matched}", STYLE_DIM)
            return
        self._append_log("/microphone [list|use <device>]", STYLE_DIM)

    # -- keys ------------------------------------------------------------
    def on_key(self, event):
        if event.key == "tab":
            if self.chat_input is None:
                return
            if not self.chat_input.has_focus:
                # Tab lands in the chat line: never let Textual's default
                # focus cycling strand typing on an invisible widget.
                self.chat_input.focus()
            else:
                value = self.chat_input.value
                new_value, self._completion_index = tui_commands.complete_command(
                    value, self._completion_index
                )
                if new_value != value:
                    self._set_chat_value(new_value)
            # without prevent_default, App._on_key still runs focus_next
            # (MRO dispatch ignores event.stop) and steals focus from the input
            event.prevent_default()
            event.stop()
        elif event.key in ("up", "down"):
            if self.chat_input is None or not self.chat_input.has_focus:
                return
            delta = -1 if event.key == "up" else 1
            value = self._browse_history(delta)
            if value is not None and value != self.chat_input.value:
                self._set_chat_value(value)
            event.prevent_default()
            event.stop()

    def _set_chat_value(self, text):
        """Programmatic input set: value + cursor, suppressing the next
        Input.Changed so completion/history navigation state survives."""
        self._suppress_changed = True
        self.chat_input.value = text
        self.chat_input.cursor_position = len(text)

    def _browse_history(self, delta):
        self._history_index, self._history_draft, value = tui_commands.history_browse(
            self._chat_history,
            self._history_index,
            self._history_draft,
            self.chat_input.value,
            delta,
        )
        return value

    def on_input_changed(self, event):
        if not self._suppress_changed:
            self._completion_index = 0
            self._history_index = -1
        hints = self._safe_query("#command-hints", CommandHints)
        if hints is not None:
            hints.update_for(event.value)
        self._suppress_changed = False
        if event.value and not event.value.startswith("/"):
            self._touch_typing()

    def _touch_typing(self):
        """Record typing activity with debouncing; nudges near-boundary sleep."""
        now = time.monotonic()
        self._typing_last = now
        activity_label = self._safe_query("#activity", ActivityLabel)
        if activity_label is not None:
            activity_label.show("listening…")
        if self._typing_timer is not None:
            self._typing_timer.stop()
        self._typing_timer = self.set_timer(0.6, self._end_typing)
        if now - getattr(self, "_typing_reported", 0) > 4.0:
            self._typing_reported = now
            try:
                if self.org is not None:
                    nudged = self.org.typing_activity()
                    if nudged:
                        self._toast("your typing woke it")
            except Exception as exc:  # noqa: BLE001 — typing is best-effort
                logger.debug("typing activity failed: %s", exc)

    def _end_typing(self):
        activity_label = self._safe_query("#activity", ActivityLabel)
        if activity_label is not None:
            activity_label.clear()
        self._typing_timer = None

    # -- voice health ------------------------------------------------------
    def _probe_voice(self):
        """Probe LLM backend reachability off the UI thread (noop while one
        is already in flight); the arena reads the cached result."""
        if not self._probing_voice:
            self._probing_voice = True
            self._probe_voice_worker()

    @work(thread=True)
    def _probe_voice_worker(self):
        try:
            llmclient.probe_voice()
        finally:
            self._probing_voice = False
        self.call_from_thread(self._announce_voice)

    def _announce_voice(self):
        """Tell the user once per voice-state flip how the organism speaks."""
        state = llmclient.voice_status()
        if state != self._voice_announced:
            self._voice_announced = state
            if state == "offline":
                self._append_log(
                    "inner voice: offline — speaking from my bones (local fallback)",
                    STYLE_DIM,
                )
                self.notify("inner voice offline — local fallback", severity="warning")
            elif state == "online":
                backend = llmclient.llm_backend()
                label = "llama.cpp" if backend == "llama_cpp" else "ollama"
                self._append_log(f"inner voice: online ({label})", STYLE_DIM)
                self.notify(f"inner voice online ({label})")
        self.refresh_status()

    # -- spoken voice (piper tts) ------------------------------------------
    @work(thread=True)
    def _voice_download(self, name):
        """Download a piper voice from huggingface in the background, then
        adopt it. Failure just logs — the current voice is kept."""
        self.call_from_thread(
            self._append_log,
            f"downloading voice {name} (this can take a minute)…",
            STYLE_DIM,
        )
        model = speech.download_voice(name)
        if model is None:
            self.call_from_thread(
                self._append_log,
                f"/voice get: couldn't fetch {name!r} — names look like "
                "en_US-lessac-medium, see "
                "huggingface.co/rhasspy/piper-voices",
                STYLE_WARN,
            )
            self.call_from_thread(
                self.show_toast, f"Voice download failed: {name}"
            )
            return
        speech.set_voice(name)
        self.call_from_thread(
            self._append_log, f"voice ready: {name}", STYLE_LEARNED, True
        )
        if speech.enabled:
            speech.say("This is my new voice.")

    # -- ticks -----------------------------------------------------------
    def _on_tick(self):
        for event in self.org.tick(1.0):
            self._render_event(event)
        if self._busy():
            self._busy_frame = (self._busy_frame + 1) % 3
        self._refresh_views()
        self.refresh_status()

    def _busy(self):
        return (
            self._narrating
            or self._responding
            or self._self_talking
            or self._group_responding
        )

    def _refresh_views(self):
        self._mind_text = tui_views.mind_view(self.org)
        self._memory_text = tui_views.memory_view(self.org)
        self.query_one("#mind", Static).update(tui_views.mind_renderable(self.org))
        self.query_one("#memory", Static).update(tui_views.memory_renderable(self.org))
        self.query_one("#inner", Static).update(tui_views.inner_renderable(self.org))
        text, self._cells_grid = tui_views.cells_layout(self.org)
        self.query_one("#cells", Static).update(text)
        self._update_mutation_banner()

    def _update_mutation_banner(self):
        pending = extensions.registry().get("pending")
        banner = self._safe_query("#mutation-banner", MutationBanner)
        if not isinstance(banner, MutationBanner):
            return
        if pending:
            summary = tui_views._pending_proposal(self.org) or f"pending {pending.get('kind', 'patch')}"
            banner.query_one("#mutation-summary", Static).update(f"Pending patch: {summary}")
            banner.styles.display = "block"
        else:
            banner.styles.display = "none"

    def _show_mutation_why(self):
        pending = extensions.registry().get("pending")
        if not pending:
            return
        why = pending.get("why", "no explanation provided")
        self.notify(f"patch reason: {why}", timeout=5)

    def _render_event(self, event):
        """Render one engine event into the log."""
        kind = event["kind"]
        if kind == "state":
            to = event["to"]
            if to == "dead":
                self._append_log("the organism has faded.", STYLE_WARN, stamp=True)
                self.notify("the organism has faded", severity="error")
                self._maybe_narrate()
            else:
                self._append_log(
                    f"— the organism drifts to {to} —", STYLE_DIM, stamp=True
                )
        elif kind == "dream":
            combos = event["combos"]
            if combos:
                self._append_log("dream: " + ", ".join(combos), STYLE_DREAM)
            else:
                self._append_log("dreams: (none promoted)", STYLE_DIM)
        elif kind == "beliefs":
            learned = ", ".join(f"{o}:{a}={v}" for (o, a, v) in event["new"])
            self._append_log(f"new beliefs: {learned}", STYLE_LEARNED)
        elif kind == "sense":
            self._append_log(
                f"the host strains (distress +{event['distress']:.2f})", STYLE_WARN
            )
        elif kind == "stress":
            level = "high" if event["band"] == 1 else "critical"
            self._append_log(f"stress rising: {level}", STYLE_WARN)
        elif kind == "mental":
            if event["insane"]:
                self._append_log(
                    "the organism's mind comes apart: incoherent, insane",
                    STYLE_WARN,
                    stamp=True,
                )
                self.notify("the organism has gone insane", severity="error")
            else:
                self._append_log(
                    "the organism's thoughts settle back into coherence",
                    STYLE_DIM,
                    stamp=True,
                )
        elif kind == "mood":
            mood = event["mood"]
            style = (
                STYLE_WARN
                if mood in ("hurt", "anxious", "insane")
                else STYLE_LEARNED
                if mood in ("grateful", "curious")
                else STYLE_DIM
            )
            self._append_log(f"mood: {mood}", style)
        elif kind == "learned":
            self._append_log(f"learned: {event['text']}", STYLE_LEARNED, stamp=True)
            self.notify(f"learned: {event['text']}")
        elif kind == "want_goal":
            self._form_goal()
        elif kind == "want_diary":
            self._write_diary()
        elif kind == "goal":
            self._append_log(
                f"goal completed: {event['text']}", STYLE_LEARNED, stamp=True
            )
            self.notify(f"goal completed: {event['text']}")
        elif kind == "want_reflect":
            self._reflect()

    def refresh_status(self):
        """Render the custom bottom bar: activity on the left, compact
        counters and keyboard shortcuts on the right."""
        m = self.org.metrics()
        if self._mud_game is not None:
            playing = " · 🗡 mud (paused)" if self._mud_paused else " · 🗡 mud"
        else:
            playing = ""
        if self._group is not None:
            playing += f" · 👥 group ({len(self._group.names())})"
        text = (
            f"{m.belief_count} beliefs · {m.rule_count} rules · "
            f"inner voice {llmclient.voice_status()}{playing}  │  "
            "ctrl+p palette · F1 help · F2-F8 tabs · ctrl+q quit "
            "(or F10, ctrl+c×2, /quit)"
        )
        self._bottombar_text = text
        if text != self._rendered_bottombar_text:
            self._rendered_bottombar_text = text
            bottombar = self._safe_query("#bottombar-text", Static)
            if bottombar is not None:
                bottombar.update(text)
        self._update_quick_actions()

    def _update_quick_actions(self):
        qa = self._safe_query("#quick-actions", QuickActions)
        if not isinstance(qa, QuickActions):
            return
        sleep_btn = qa.query_one("#qa-sleep", Button)
        sleep_btn.label = (
            "Wake" if self.org.lifecycle.state == "sleep" else "Sleep / Wake"
        )
        voice_btn = qa.query_one("#qa-voice", Button)
        voice_btn.label = f"Voice: {'on' if speech.enabled else 'off'}"
        mud_btn = qa.query_one("#qa-mud", Button)
        mud_btn.label = "MUD: on" if self._mud_game is not None else "MUD: off"

    def set_activity(self, text):
        """Show a transient activity message in the status bar."""
        label = self._safe_query("#activity", ActivityLabel)
        if label is not None:
            label.show(text)

    def clear_activity(self):
        """Hide the transient activity message."""
        label = self._safe_query("#activity", ActivityLabel)
        if label is not None:
            label.clear()

    @property
    def activity_text(self):
        """Current activity label text (for tests)."""
        label = self._safe_query("#activity", ActivityLabel)
        if label is None:
            return ""
        return str(getattr(label, "_Static__content", "") or "")

    def show_toast(self, message, duration=3.0):
        """Show a transient toast at the bottom of the screen."""
        toast = self._safe_query("#toast", Toast)
        if toast is not None:
            toast.show(message, duration=duration)

    # -- log ---------------------------------------------------------------
    def _safe_query(self, selector, cls):
        """query_one that returns None instead of raising when the widget is
        not mounted (unit tests drive handlers without a screen)."""
        try:
            return self.query_one(selector, cls)
        except (ScreenStackError, NoMatches):
            return None

    def _stamp(self):
        return datetime.now(UTC).astimezone().strftime("%H:%M")

    def _append_log(self, text, style=None, stamp=False):
        """Append one styled line to the scrollable log (markup-escaped),
        optionally prefixed with a dim HH:MM timestamp."""
        line = escape(text)
        if style:
            line = f"[{style}]{line}[/{style}]"
        if stamp:
            line = f"[dim]{self._stamp()}[/dim] {line}"
        dreams = self._safe_query("#dreams", RichLog)
        if dreams is not None:
            dreams.write(line)

    def _org_name(self):
        """Card title for the organism: its learned name (the user can give
        it one with 'your name is …'), else its nursery dir name."""
        return self.org.store.belief_value(
            "self", "name", self.org.dir_path.name or "replicanta"
        )

    def _log_chat(self, role, text, stamp=True):
        if role == "user":
            self._write_card("you", text, STYLE_USER, stamp=stamp)
        else:
            self._write_card(self._org_name(), text, STYLE_ORG, stamp=stamp)

    def _write_card(self, who, text, border_style, stamp=True):
        """One conversation message as a padded card (role-colored border,
        timestamped title), preceded by a blank line so exchanges breathe.
        Content is a plain Rich Text — organism output may contain markup
        metacharacters."""
        ts = self._stamp() if stamp else None
        card = tui_views.chat_card(who, text, timestamp=ts, border_style=border_style)
        log = self.query_one("#dreams", RichLog)
        log.write("")
        log.write(card)

    # -- goals + artifacts -------------------------------------------------
    @work(thread=True)
    def _form_goal(self):
        org = self.org  # capture: a swap mid-debate drops the delivery
        text = None
        try:
            self.call_from_thread(self._pending_show, "org is setting itself a goal")
            text = voice.form_goal(org)
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "goal", exc)
        if text is not None and org is self.org:
            self.call_from_thread(self._set_goal, text)

    def _set_goal(self, text):
        self._pending_hide()
        self.org.add_goal(text)
        self._append_log(f"goal: {text}", STYLE_LEARNED, stamp=True)
        self.notify(f"new goal: {text}")
        self.refresh_status()

    @work(thread=True)
    def _write_diary(self):
        org = self.org  # capture: a swap mid-debate drops the delivery
        entry = None
        try:
            self.call_from_thread(self._pending_show, "org is writing in its diary")
            entry = voice.diary_entry(org)
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "diary", exc)
        if entry is not None and org is self.org:
            self.call_from_thread(self._set_diary, entry)

    def _set_diary(self, entry):
        self._pending_hide()
        self.org.write_diary(entry)
        self._write_card(f"{self._org_name()} · diary", entry, STYLE_DREAM)
        self._append_log(
            "diary: entry saved (artifacts/diary.md)", STYLE_DIM, stamp=True
        )
        self.refresh_status()

    @work(thread=True)
    def _reflect(self):
        org = self.org  # capture: a swap mid-debate drops the delivery
        result = None
        try:
            self.call_from_thread(self._pending_show, "org is reflecting")
            result = voice.reflect(org)
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "reflection", exc)
        if result is not None and org is self.org:
            self.call_from_thread(self._set_reflection, result)

    def _set_reflection(self, result):
        self._pending_hide()
        if result["action"] == "none":
            return
        if result["action"] == "proposal":
            entry = result["entry"]
            applied = result.get("applied")
            auto = self.org.store.auto_apply_patches
            if applied is not None:
                self.org.store.remember("skill", f"patch applied ({entry['kind']})")
                self._append_log(
                    f"patch applied ({entry['kind']}) — live now, no restart needed",
                    STYLE_LEARNED,
                    stamp=True,
                )
                self.notify(f"patch applied ({entry['kind']})", severity="information")
            else:
                self.org.store.remember("skill", f"proposed a patch ({entry['kind']})")
            if entry["kind"] == "pattern":
                detail = f"{entry['regex']} -> {entry['template']}"
            else:
                detail = entry.get("text", "")
            if auto:
                body = (
                    f"{detail}\nwhy: {entry.get('why', '')}\n"
                    "auto-apply is on; toggle with /auto-apply off"
                )
            else:
                body = (
                    f"{detail}\nwhy: {entry.get('why', '')}\n"
                    "/approve to accept · /reject to discard"
                )
            self._write_card(f"{self._org_name()} · proposes a patch", body, "yellow")
            self.notify(
                "patch proposed — /approve or /reject"
                if not auto
                else "patch applied automatically",
                severity="warning" if not auto else "information",
            )
            self.refresh_status()
            return
        self.org.store.remember("skill", f"{result['action']} skill: {result['name']}")
        self._append_log(
            f"skill {result['action']}: {result['name']}", STYLE_LEARNED, stamp=True
        )
        self.notify(f"skill {result['action']}: {result['name']}")
        self.refresh_status()

    # -- pending (live reply region) --------------------------------------
    def _pending_show(self, label):
        self._pending_text = ""
        self._pending_visible = True
        self.query_one("#pending", Static).update(f"{label}…")

    def _pending_token(self, token):
        self._pending_text += token
        self.query_one("#pending", Static).update(self._pending_text)

    def _pending_hide(self):
        self._pending_visible = False
        self.query_one("#pending", Static).update("")

    def _worker_error(self, what, exc):
        self._pending_hide()
        self._append_log(f"{what} failed: {exc}", STYLE_WARN)
        self.refresh_status()

    # -- narration -------------------------------------------------------
    def _maybe_narrate(self):
        """Route the periodic voice: self-dialogue when toggled on and
        awake; otherwise ordinary narration, sometimes swapped for a
        curious question directed at the user (never twice in a row)."""
        if self._self_talk_on and self.org.lifecycle.state == "wake":
            self._maybe_self_talk()
            return
        if self._narrating:
            return
        self._narrating = True
        if (
            self.org.lifecycle.state == "wake"
            and not self._last_was_question
            and self._rng.random() < ASK_USER_ODDS
        ):
            self._last_was_question = True
            self.refresh_status()
            self._ask_user()
            return
        self._last_was_question = False
        self.refresh_status()
        self._narrate()

    @work(thread=True)
    def _ask_user(self):
        org = self.org  # capture: a swap mid-debate drops the delivery
        question = None
        self.call_from_thread(self.set_activity, "org is wondering")
        try:
            self.call_from_thread(self._pending_show, "org is wondering")
            question = voice.ask_user(
                org,
                on_token=lambda tok: self.call_from_thread(self._pending_token, tok),
            )
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "question", exc)
        finally:
            self.call_from_thread(self.clear_activity)
            self._narrating = False
        if question is not None and org is self.org:
            self.call_from_thread(self._set_user_question, question)

    def _set_user_question(self, question):
        self._pending_hide()
        self.org.store.record_chat("org", question)
        self._write_card(self._org_name(), question, STYLE_ORG)
        speech.say(question)
        self.refresh_status()

    # -- self-talk ---------------------------------------------------------
    def _maybe_self_talk(self):
        if not self._self_talking:
            self._self_talking = True
            self.refresh_status()
            self._self_talk()

    @work(thread=True)
    def _self_talk(self):
        org = self.org  # capture: a swap mid-debate drops the delivery
        answer = None
        self.call_from_thread(self.set_activity, "org is talking to itself")
        try:
            self.call_from_thread(self._pending_show, "org is asking itself")
            question = voice.self_ask(org)
            if org is not self.org:
                return
            self.call_from_thread(self._pending_hide)
            self.call_from_thread(self._set_self_question, question)
            self.call_from_thread(self._pending_show, "org is answering")
            answer = voice.self_answer(
                org,
                question,
                on_token=lambda tok: self.call_from_thread(self._pending_token, tok),
            )
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "self-talk", exc)
        finally:
            self.call_from_thread(self.clear_activity)
            self._self_talking = False
        if answer is not None and org is self.org:
            self.call_from_thread(self._set_self_answer, answer)

    def _set_self_question(self, question):
        self.org.store.record_chat("org", question)
        self._write_card("self", question, "dim yellow")
        speech.say(question)

    def _set_self_answer(self, answer):
        self._pending_hide()
        self.org.store.record_chat("org", answer)
        # nested under its question so the exchange reads as a dialogue
        self._append_log(f"  ↳ {answer}", STYLE_SELF)
        speech.say(answer)
        self.refresh_status()

    @work(thread=True)
    def _narrate(self):
        org = self.org  # capture: a swap mid-debate drops the delivery
        text = None
        self.call_from_thread(self.set_activity, "org is musing")
        try:
            self.call_from_thread(self._pending_show, "org is musing")
            text = voice.narrate(org)
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "narration", exc)
        finally:
            self.call_from_thread(self.clear_activity)
            self._narrating = False
        if text is not None and org is self.org:
            self.call_from_thread(self._log_narration, text)

    def _log_narration(self, text):
        self._pending_hide()
        # record the musing so later prompts (and the cross-cycle repeat
        # gate) know what the voice already said — this is what keeps the
        # idle voice from circling the same thought
        self.org.store.record_chat("org", text)
        self._write_card(self._org_name(), text, STYLE_ORG)
        speech.say(text)
        self.refresh_status()

    # -- chat line -------------------------------------------------------
    def on_input_submitted(self, event):
        text = event.value.strip()
        self.query_one("#chat", Input).value = ""
        tui_commands.history_push(self._chat_history, text)
        if text.startswith("/"):
            self.handle_command(text)
        elif text:
            self.handle_chat(text)

    def handle_command(self, cmd):
        """Parse and dispatch a slash-command line from the chat input."""
        parts = cmd.split()
        name = parts[0]
        try:
            self._dispatch(name, parts)
        except (ValueError, IndexError) as exc:
            # a mistyped argument must never kill the input handler
            self._append_log(f"{name}: {exc}", STYLE_WARN)

    def _dispatch(self, name, parts):
        if name == "/chaos":
            if len(parts) != 2:
                self._append_log(
                    f"/chaos needs a number 0-1 (now {self.org.store.chaos:.2f})",
                    STYLE_DIM,
                )
                return
            value = float(parts[1])
            if not 0.0 <= value <= 1.0:
                raise ValueError("chaos must be between 0 and 1")
            self.org.store.chaos = value
            self._append_log(f"chaos: {value:.2f}", STYLE_DIM)
            self.refresh_status()
        elif name == "/focus" and len(parts) == 2:
            self.org.window.focus(parts[1])
            self.org.store.attention = self.org.window.pairs
            self._append_log(f"attention locked on {parts[1]}", STYLE_DIM)
        elif name == "/focus":
            self.org.window.focus(None)
            self._append_log("attention floating free", STYLE_DIM)
        elif name == "/sleep":
            for event in self.org.force_state("sleep"):
                self._render_event(event)
        elif name == "/wake":
            for event in self.org.force_state("wake"):
                self._render_event(event)
        elif name == "/revive":
            if self.org.revive():
                self._append_log(
                    "revived: the organism stirs back into existence.", STYLE_DIM
                )
                self._maybe_narrate()
            else:
                self._append_log(
                    f"/revive: it is not faded (state {self.org.lifecycle.state}).",
                    STYLE_DIM,
                )
        elif name == "/stats":
            m = self.org.metrics()
            s = self.org.store
            self._append_log(
                f"stats: beliefs={m.belief_count} rules={m.rule_count} "
                f"depth={m.total_depth} score={m.score():.1f}",
                STYLE_DIM,
            )
            self._append_log(
                f"mental: arousal={s.arousal:.2f} "
                f"rationality={s.rationality:.2f} "
                f"irrationality={s.irrationality:.2f} "
                f"insane={s.insane}",
                STYLE_DIM,
            )
            for line in activity.summary_lines(self.org.store):
                self._append_log(line, STYLE_DIM)
        elif name == "/save":
            self.action_save_now()
        elif name == "/export":
            try:
                dest = self._export_chat(parts[1] if len(parts) > 1 else None)
                self._append_log(f"— chat exported to {dest} —", STYLE_DIM, stamp=True)
            except OSError as exc:
                self._append_log(f"— export failed: {exc} —", STYLE_WARN, stamp=True)
        elif name == "/think":
            self.action_think_now()
        elif name == "/listen":
            self._toggle_listen()
        elif name == "/microphone":
            self._microphone(parts[1:])
        elif name == "/look":
            self._look_now()
        elif name == "/camera":
            self._camera(parts[1:])
        elif name == "/mud":
            self._mud_command(parts[1:])
        elif name == "/reload":
            self.org.hooks.reload()
            count = len(self.org.hooks.scripts)
            self._append_log(
                f"lua hooks reloaded ({count} script{'s' if count != 1 else ''})",
                STYLE_DIM,
            )
        elif name == "/lua":
            if len(parts) != 2:
                names = ", ".join(s.name for s in self.org.hooks.scripts)
                self._append_log(
                    f"/lua needs a script name (scripts/: {names or 'none'})", STYLE_DIM
                )
                return
            self._append_log(self.org.hooks.run(parts[1], self.org), STYLE_DIM)
        elif name == "/organisms":
            names = nursery.list_organisms(self.root)
            current = self.org.dir_path.name
            listing = (
                ", ".join(f"*{n}" if n == current else n for n in names) or "(none)"
            )
            self._append_log(f"organisms: {listing}  (* = current)", STYLE_DIM)
        elif name == "/group":
            self._group_command(parts[1:])
        elif name == "/new":
            new_name = parts[1] if len(parts) == 2 else nursery.next_name(self.root)
            try:
                nursery.create(self.root, new_name, Path(self.root) / "organism.scl")
            except (ValueError, OSError) as exc:
                self._append_log(f"/new: {exc}", STYLE_WARN)
            else:
                self._swap_to(new_name)
        elif name == "/swap":
            if len(parts) != 2:
                self._append_log("/swap needs a name — /organisms to list.", STYLE_DIM)
                return
            if parts[1] not in nursery.list_organisms(self.root):
                names = ", ".join(nursery.list_organisms(self.root)) or "(none)"
                self._append_log(
                    f"/swap: no organism {parts[1]!r} — have: {names}", STYLE_WARN
                )
                return
            self._swap_to(parts[1])
        elif name == "/voice":
            args = parts[1:]
            if not args or args[0] in ("on", "off"):
                if args:
                    speech.set_enabled(args[0] == "on")
                else:
                    speech.set_enabled(not speech.enabled)
                state = "on" if speech.enabled else "off"
                if speech.enabled and not speech.available():
                    self._append_log(
                        f"spoken voice {state}, but no piper model at "
                        f"{speech.model_path()} — staying mute "
                        f"(/voice get en_US-lessac-medium)",
                        STYLE_WARN,
                    )
                elif speech.enabled:
                    self._append_log(
                        "spoken voice on — the organism speaks aloud (piper tts)",
                        STYLE_DIM,
                    )
                    speech.say("I can speak now.")
                else:
                    self._append_log("spoken voice off", STYLE_DIM)
                self.refresh_status()
            elif args[0] == "list":
                voices = speech.list_voices()
                active = speech.voice_name()
                listing = (
                    ", ".join(f"*{v}" if v == active else v for v in voices)
                    or "(none — /voice get en_US-lessac-medium)"
                )
                self._append_log(f"voices: {listing}  (* = active)", STYLE_DIM)
            elif args[0] == "use" and len(args) == 2:
                if speech.set_voice(args[1]):
                    self._append_log(f"voice: {speech.voice_name()}", STYLE_DIM)
                    speech.say("This is my new voice.")
                else:
                    have = ", ".join(speech.list_voices()) or "(none)"
                    self._append_log(
                        f"/voice use: no voice {args[1]!r} — have: {have}. "
                        f"/voice get {args[1]} downloads it",
                        STYLE_WARN,
                    )
            elif args[0] == "get" and len(args) == 2:
                self._voice_download(args[1])
            else:
                self._append_log(
                    "/voice [on|off] · /voice list · /voice use name · /voice get name",
                    STYLE_DIM,
                )
        elif name == "/self-talk":
            self._self_talk_on = not self._self_talk_on
            if self._self_talk_on:
                self._append_log(
                    "self-talk on — the organism may speak to itself.", STYLE_DIM
                )
                if self.org.lifecycle.state == "wake":
                    self._maybe_self_talk()
            else:
                self._append_log("self-talk off", STYLE_DIM)
        elif name == "/approve":
            entry = extensions.approve(
                self.org.dir_path / "artifacts" / "extensions.json"
            )
            if entry:
                self.org.store.remember("skill", f"patch applied ({entry['kind']})")
                self._append_log(
                    f"patch applied ({entry['kind']}) — live now, no restart needed",
                    STYLE_LEARNED,
                    stamp=True,
                )
            else:
                self._append_log("/approve: no pending patch.", STYLE_DIM)
        elif name == "/reject":
            entry = extensions.reject(
                self.org.dir_path / "artifacts" / "extensions.json"
            )
            if entry:
                self.org.store.remember("skill", f"patch rejected ({entry['kind']})")
                self._append_log(
                    f"patch rejected ({entry['kind']})", STYLE_DIM, stamp=True
                )
            else:
                self._append_log("/reject: no pending patch.", STYLE_DIM)
        elif name == "/auto-apply":
            args = parts[1:]
            if args and args[0] in ("on", "off"):
                self.org.store.auto_apply_patches = args[0] == "on"
                self.org.store.dirty = True
                state = "on" if self.org.store.auto_apply_patches else "off"
                self._append_log(f"auto-apply patches: {state}", STYLE_DIM)
            else:
                state = "on" if self.org.store.auto_apply_patches else "off"
                self._append_log(
                    f"auto-apply patches is {state} — use /auto-apply on|off", STYLE_DIM
                )
        elif name == "/revert":
            entry = extensions.revert_last(
                self.org.dir_path / "artifacts" / "extensions.json"
            )
            if entry:
                self.org.store.remember("skill", f"patch reverted ({entry['kind']})")
                self._append_log(
                    f"patch reverted ({entry['kind']})", STYLE_LEARNED, stamp=True
                )
            else:
                self._append_log("/revert: no applied patches yet.", STYLE_DIM)
        elif name == "/quit":
            self.action_quit()
        elif name == "/help":
            self.action_help()
        elif name == "/git":
            self._git_command(parts[1:])
        elif name == "/persona":
            self._persona_command(parts[1:])
        elif name == "/modules":
            self._modules_command(parts[1:])
        else:
            self._append_log(f"unknown: {name} (try /help)", STYLE_WARN)
            self.show_toast(f"Invalid command: {name}")

    def _git_command(self, args):
        if not args or args[0] == "status":
            self._append_log(self.org.git_status(), STYLE_DIM)
        elif args[0] == "on":
            self.org.git_enable()
            self._append_log("git sensing on", STYLE_DIM)
        elif args[0] == "off":
            self.org.git_disable()
            self._append_log("git sensing off", STYLE_DIM)
        else:
            self._append_log("/git [on|off|status]", STYLE_DIM)

    def _persona_command(self, args):
        svc = getattr(self.org, "persona_service", None)
        if svc is None:
            self._append_log("persona service unavailable", STYLE_WARN)
            return
        if not args or args[0] == "list":
            active = svc.active()
            names = svc.list()
            line = "personas: " + ", ".join(
                f"*{n}" if active and active["name"] == n else n for n in names
            )
            self._append_log(line, STYLE_DIM)
        elif args[0] == "off":
            svc.deactivate()
            self._append_log("persona cleared", STYLE_DIM)
        else:
            svc.activate(args[0])
            self._append_log(f"persona: {args[0]}", STYLE_DIM)

    def _modules_command(self, args):
        if args and args[0] != "manage":
            self._append_log("/modules [manage]", STYLE_DIM)
            return
        self.action_modules()

    def handle_chat(self, text):
        """Route ordinary user chat to MUD, group chat, or the organism."""
        self._log_chat("user", text)
        if self._mud_game is not None:
            command = mud.parse_player_command(text)
            if command is not None:
                # a direct move: execute now, not a hint, not chat. Bump
                # the turn generation so an organism move chosen before
                # this command cannot land after it.
                self._mud_turn_gen += 1
                self._mud_apply(self._mud_game, command, actor="user")
                return
            self._mud_turn_gen += 1  # hints invalidate in-flight moves too
            self._mud_hint = text  # shout a nudge into the next move
        if self._group is not None:
            # Group chat lines go into the shared transcript and member
            # memory (GroupChat.broadcast records them as "group" episodes).
            # They must not be written to each organism's individual
            # one-on-one chat_log.
            self._maybe_group_respond(text)
            return
        for event in self.org.hear(text):
            self._render_event(event)
        self._maybe_respond(text)

    def _maybe_respond(self, text):
        if not self._responding:
            self._responding = True
            self.refresh_status()
            self._respond(text)

    @work(thread=True)
    def _respond(self, text):
        org = self.org  # capture: a swap mid-debate drops the delivery
        reply = None
        self.call_from_thread(self.set_activity, "org is thinking")
        try:
            self.call_from_thread(self._pending_show, "org is thinking")
            reply = voice.respond(
                org,
                text,
                on_token=lambda tok: self.call_from_thread(self._pending_token, tok),
            )
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "reply", exc)
        finally:
            self.call_from_thread(self.clear_activity)
            self._responding = False
        if reply is not None and org is self.org:
            self.call_from_thread(self._set_reply, reply)

    # -- group chat -------------------------------------------------------
    GROUP_STYLES: ClassVar[list[str]] = [
        "green",
        "yellow",
        "magenta",
        "cyan",
        "bright_blue",
        "bright_magenta",
    ]

    def _group_style(self, name):
        """Stable per-member card color while the group is active."""
        idx = self._group.names().index(name) if self._group else 0
        return self.GROUP_STYLES[idx % len(self.GROUP_STYLES)]

    def _group_command(self, args):
        """/group start a b [c…] | /group stop | bare /group for status."""
        if not args:
            if self._group is None:
                self._append_log(
                    "no active group — /group start "
                    + " ".join(nursery.list_organisms(self.root)),
                    STYLE_DIM,
                )
            else:
                self._append_log(
                    f"group chat: {', '.join(self._group.names())} "
                    f"({len(self._group.transcript)} messages)",
                    STYLE_DIM,
                )
            return
        if args[0] == "stop":
            if self._group is None:
                self._append_log("no active group.", STYLE_DIM)
                return
            names = ", ".join(self._group.names())
            self.action_save_now()
            self._group = None
            self._append_log(f"— group chat ended ({names}) —", STYLE_DIM, stamp=True)
            self.refresh_status()
            return
        if args[0] != "start":
            self._append_log("usage: /group start a b [c…] | /group stop", STYLE_DIM)
            return
        names = args[1:]
        if names == ["all"]:
            names = nursery.list_organisms(self.root)
        else:
            # nursery group names expand to their members (an organism of
            # the same name wins — the specific entity over the collection)
            groups = nursery.load_groups(self.root)
            known_orgs = set(nursery.list_organisms(self.root))
            expanded = []
            for n in names:
                if n in groups and n not in known_orgs:
                    expanded.extend(m for m in groups[n] if m not in expanded)
                else:
                    expanded.append(n)
            names = expanded
        # the organism you live with always takes a seat in the group
        current = self.org.dir_path.name
        if current not in names:
            names = [current] + list(names)
        known = set(nursery.list_organisms(self.root))
        missing = [n for n in names if n not in known]
        if missing:
            self._append_log(
                f"/group: unknown organisms or groups: {', '.join(missing)}", STYLE_WARN
            )
            return
        members = {}
        for n in names:
            if n == self.org.dir_path.name:
                members[n] = self.org
            else:
                org = Organism(nursery.organism_dir(self.root, n), **self._spawn)
                org.load()
                members[n] = org
        try:
            self._group = groupchat.GroupChat(members)
        except ValueError as exc:
            self._append_log(f"/group: {exc}", STYLE_WARN)
            return
        self._append_log(
            f"— group chat started: {', '.join(self._group.names())} — "
            "everything you type is broadcast; address one member with "
            "'name: …' or '@name …'; /group stop to end —",
            STYLE_DIM,
            stamp=True,
        )
        self.refresh_status()

    def _maybe_group_respond(self, text):
        if not self._group_responding:
            self._group_responding = True
            self.refresh_status()
            self._group_respond(text)

    @work(thread=True)
    def _group_respond(self, text):
        group = self._group  # capture: /group stop mid-broadcast drops it
        if group is None:
            return
        utterances = None
        self.call_from_thread(self.set_activity, "group is thinking")
        try:
            self.call_from_thread(self._pending_show, "group is thinking")
            utterances = group.broadcast(text)
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "group reply", exc)
        finally:
            self.call_from_thread(self.clear_activity)
            self._group_responding = False
        if utterances is not None and group is self._group:
            self.call_from_thread(self._deliver_group, utterances)

    def _deliver_group(self, utterances):
        self._pending_hide()
        for name, reply in utterances:
            # Group replies are shared transcript state, not part of an
            # individual organism's one-on-one chat history. Memory is
            # already recorded by GroupChat.broadcast; don't pollute the
            # per-organism chat_log.
            self._write_card(name, reply, self._group_style(name))
        self.refresh_status()

    def _set_reply(self, reply):
        self._pending_hide()
        self._log_chat("org", reply)
        speech.say(reply)
        self.refresh_status()


def main():
    """CLI entry point: parse args, prepare the nursery, and run TUI or web UI."""
    import argparse

    parser = argparse.ArgumentParser(description="Replicanta TUI")
    parser.add_argument("--dir", default=str(Path(__file__).parent))
    parser.add_argument("--org", default=None, help="organism name in the nursery")
    parser.add_argument("--wake", type=int, default=300)
    parser.add_argument("--sleep", type=int, default=60)
    parser.add_argument("--chaos", type=float, default=0.5)
    parser.add_argument(
        "-web",
        "--web",
        action="store_true",
        help="launch the local Glasshouse web UI",
    )
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8765, help="Glasshouse port")
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open a browser with --web"
    )
    args = parser.parse_args()
    root = Path(args.dir)
    nursery.migrate(root)
    name = args.org or nursery.current(root)
    if not nursery.NAME_RE.match(name):
        parser.error(f"invalid organism name: {name!r}")
    org_dir = nursery.organism_dir(root, name)
    if not org_dir.exists():
        nursery.create(root, name, root / "organism.scl")
    spawn = {
        "wake_seconds": args.wake,
        "sleep_seconds": args.sleep,
        "chaos": args.chaos,
    }
    org = Organism(org_dir, **spawn)
    org.load()
    if args.web:
        from replicanta import web

        web.run(
            root,
            org,
            spawn,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
        return
    OrganismApp(org, root, spawn).run()


if __name__ == "__main__":
    main()
