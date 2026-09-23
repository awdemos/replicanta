"""The Textual app shell: OrganismApp wires the organism core (organism.py),
the thought arena (arena.py) and the per-subsystem controllers
(tui_controllers.py) to the terminal, delegating pure rendering/parsing to
tui_views.py and tui_commands.py."""

import contextlib
import logging
import os
import random
import tempfile
import threading
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import work
from textual.actions import SkipAction
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.command import Hit, Matcher, Provider
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    RichLog,
    Static,
)

try:
    import pyperclip
except Exception:  # noqa: BLE001 — optional convenience, not load-bearing
    pyperclip = None
from textual.widgets.option_list import Option

from replicanta import (
    camera,
    extensions,
    fileutil,
    groupchat,
    listen,
    nursery,
    rdd,
    speech,
    telemetry,
    tui_commands,
    tui_views,
    voice,
)
from replicanta.organism import Organism
from replicanta.tui_controllers import (
    DoomController,
    MudController,
    VoiceController,
    extract_doom_command,
)
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
            # Input.action_submit() is async in Textual 8 — a bare call
            # discards the coroutine and nothing ever submits. Posting
            # Submitted is what action_submit does, and it drives the
            # app's real on_input_submitted path.
            chat = self.app.chat_input
            chat.post_message(Input.Submitted(chat, chat.value))

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
                yield self._hit(name, usage, description, score=match.score, display=match.highlight)


class MutationBanner(Horizontal):
    """Sticky banner for pending extension patches with approve/reject/why."""

    def compose(self) -> ComposeResult:
        yield Static("", id="mutation-summary")
        yield Button("Approve", id="mutation-approve", variant="success")
        yield Button("Reject", id="mutation-reject", variant="error")
        yield Button("Why?", id="mutation-why")


# Sidebar badge glyphs for loaded capability modules (persona modules stay
# unbadged; capability modules get a persistent per-entity marker).
MODULE_BADGES = {
    "fly-brain": "\U0001fab0",  # 🪰
    "tendon-hand": "\U0001f9be",  # 🦾
    "visual-state": "\U0001f441",  # 👁
    "doom-ascii": "\U0001f480",  # 💀
}


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
    """Searchable slash-command palette: a centered card with a filter
    box, grouped results with the matched text highlighted, and a count
    plus key hints."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="palette"):
            yield Static("Command Palette", id="palette-title")
            yield Input(placeholder="type to filter commands…", id="palette-input")
            yield ListView(id="palette-results")
            yield Static("", id="palette-meta")

    def on_mount(self):
        # on_mount, not on_show: the input and results are composed inside
        # #palette, and on_show can fire before that subtree is queryable.
        # A caller may also dismiss the palette before its subtree mounts
        # (push then immediate dismiss), which must not raise here.
        try:
            self.query_one("#palette-input", Input).focus()
        except NoMatches:
            return
        self._refresh_palette("")

    def on_input_changed(self, event):
        self._refresh_palette(event.value)

    @staticmethod
    def _highlight(text, query):
        """Render ``text`` with the first case-insensitive ``query`` match
        reversed+bold so the eye lands on why a row matched."""
        if not query:
            return text
        low = text.lower()
        q = query.lower()
        i = low.find(q)
        if i < 0:
            return text
        return f"{text[:i]}[bold reverse u]{text[i : i + len(q)]}[/]{text[i + len(q) :]}"

    def _refresh_palette(self, query):
        results = self.query_one("#palette-results", ListView)
        self._clear_await = results.clear()  # AwaitRemove; completes on the next tick
        items = tui_commands.filter_commands(query)
        meta = self.query_one("#palette-meta", Static)
        if not items:
            results.append(ListItem(Static("[dim]no matching commands[/dim]")))
            meta.update("[dim]esc closes[/dim]")
            return
        categories = {}
        for name, usage, desc, category in items:
            categories.setdefault(category, []).append((name, usage, desc))
        count = 0
        for category in (
            "State",
            "Voice",
            "Senses",
            "MUD",
            "Organisms",
            "System",
            "Help",
        ):
            if category not in categories:
                continue
            header = ListItem(Static(f"[bold]{category}[/bold]"))
            header.disabled = True
            results.append(header)
            for name, usage, desc in categories[category]:
                count += 1
                item = ListItem(
                    Vertical(
                        Static(self._highlight(usage, query), classes="palette-usage"),
                        Static(self._highlight(desc, query), classes="palette-desc"),
                    )
                )
                item.data = name
                results.append(item)
        meta.update(f"[dim]{count} command{'s' if count != 1 else ''} · ↑↓ select · enter run · esc close[/dim]")

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


class BeingScreen(Screen):
    """Full-screen dashboard: mind, memory, inner state, and the neural
    memory grid — the former tab panes as one scrollable overlay (F3)."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close")]

    CSS = """
    BeingScreen { background: $surface; }
    #being-scroll { height: 1fr; padding: 1 2; }
    .being-header { height: 1; padding: 1 0 0 0; text-style: bold; color: $text; }
    #mind, #memory, #inner, #cells { height: auto; padding: 0 1 1 1; }
    """

    def __init__(self, org):
        """Create the dashboard for the given organism (live-refreshed
        every tick while the screen is on top)."""
        super().__init__()
        self._org = org

    def compose(self) -> ComposeResult:
        """Build the four labeled sections; content is plain renderables."""
        with VerticalScroll(id="being-scroll"):
            yield Label("MIND", classes="being-header")
            yield Static(tui_views.mind_renderable(self._org), id="mind", markup=False)
            yield Label("MEMORY", classes="being-header")
            yield Static(tui_views.memory_renderable(self._org), id="memory", markup=False)
            yield Label("INNER", classes="being-header")
            yield Static(tui_views.inner_renderable(self._org), id="inner", markup=False)
            yield Label("CELLS", classes="being-header")
            yield Static(tui_views.cells_view(self._org), id="cells", markup=False)

    def on_mount(self):
        """The app recomputes the click grid from the live organism."""
        self.app._cells_grid = tui_views.cells_layout(self._org)[1]

    def refresh_sections(self, org):
        """Re-render every section from the given (possibly swapped) organism."""
        self._org = org
        self.query_one("#mind", Static).update(tui_views.mind_renderable(org))
        self.query_one("#memory", Static).update(tui_views.memory_renderable(org))
        self.query_one("#inner", Static).update(tui_views.inner_renderable(org))
        self.query_one("#cells", Static).update(tui_views.cells_view(org))

    def action_dismiss(self):
        self.app.pop_screen()


class DoomScreen(Screen):
    """Full-screen DOOM overlay: the entity's thought stream above the
    80-column ASCII frame, with its own chat line at the bottom (the main
    screen's input can't be seen while this overlay is up). Escape closes
    the overlay; the game keeps running (wasd/qe/arrows/space play,
    /doom stop ends it).

    The movement keys are bound HERE, not on the app: app-level w/s would
    swallow everyday typing on the main screen, while screen-level bindings
    only exist while this overlay is on top. The chat Input starts
    UNFOCUSED so the game keys play; Tab focuses it for typing, and Enter
    returns focus to the game."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "close"),
        Binding("w", "press_key('w')", "forward", show=False),
        Binding("s", "press_key('s')", "back", show=False),
        Binding("a", "press_key('a')", "turn left", show=False),
        Binding("d", "press_key('d')", "turn right", show=False),
        Binding("q", "press_key('q')", "strafe left", show=False),
        Binding("e", "press_key('e')", "strafe right", show=False),
    ]

    # Never auto-focus the chat input when the overlay opens: the game keys
    # must play immediately; Tab enters typing mode explicitly.
    AUTO_FOCUS: ClassVar[str] = ""

    HINT = "esc closes — wasd/qe/arrows move — space shoots — tab types — /doom stop ends"

    CSS = """
    DoomScreen { background: $surface; }
    #doom-hint { height: 1; padding: 0 1; color: $text-muted; }
    #doom-scroll { height: 1fr; }
    # 82 = 80-column frame + 1 col padding each side: the content width must
    # be exactly 80 or Rich wraps the art mid-line and the picture shreds.
    #doom { padding: 0 1; width: 82; min-width: 82; }
    #doom-thoughts { padding: 1 2; height: auto; max-height: 10; color: $success; }
    #doom-chat {
        height: 3;
        border: none;
        border-top: solid $surface-lighten-2;
        padding: 0 1;
    }
    #doom-chat:focus { border-top: solid $accent; }
    """

    def compose(self) -> ComposeResult:
        """Build the hint line, the scrollable frame, and the chat input."""
        with Vertical(id="doom-box"):
            yield Label(self.HINT, id="doom-hint")
            # The container must never take focus: when the frame is taller
            # than the terminal (scaling 2) a focused ScrollableContainer
            # eats up/down for scrolling and the player can't walk — the
            # game keys go straight to the app bindings. The wheel still
            # scrolls an unfocused container.
            scroll = ScrollableContainer(id="doom-scroll")
            scroll.can_focus = False
            with scroll:
                yield Static("", id="doom-thoughts", markup=False)
                yield Static(
                    "Run /doom start to play DOOM (doom-ascii).",
                    id="doom",
                    markup=False,
                )
            yield Input(placeholder="tab — type here · enter — send", id="doom-chat")

    def on_mount(self):
        """Paint the current frame immediately: the controller pushed this
        screen asynchronously, after it had already computed the frame."""
        self.app._doom.refresh(force=True)

    def action_press_key(self, key: str) -> None:
        """Overlay movement keys (w/a/s/d/q/e bindings above)."""
        self.app._doom.key_command(key)

    def focus_game(self):
        """Return focus to the screen after a send so the game keys work."""
        self.app.set_focus(None)

    def show_key(self, label: str) -> None:
        """Echo the last manual key in the hint line: walking into a wall
        leaves the frame unchanged, and without this the keypress looks
        dropped."""
        hint = self.query_one("#doom-hint", Label)
        hint.update(f"{self.HINT} — you: {label}")

    def show_user_line(self, text: str) -> None:
        """Echo a submitted chat line into the thought stream — the main
        chat log sits on the screen beneath this overlay, unseen."""
        thoughts = self.query_one("#doom-thoughts", Static)
        current = str(getattr(thoughts, "_Static__content", "") or "")
        if current.startswith("> "):
            current = ""
        stamped = "\n".join(
            f"[{datetime.now(UTC).strftime('%H:%M:%S')}] you › {line}" for line in text.splitlines() if line.strip()
        )
        lines = (current.splitlines() if current else []) + stamped.splitlines()
        thoughts.update("\n".join(lines[-8:]))

    def action_dismiss(self):
        self.app.pop_screen()


class MudScreen(Screen):
    """Full-screen MUD overlay: a view of the dungeon. Input stays in the
    chat bar; escape closes the overlay."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close")]

    CSS = """
    MudScreen { background: $surface; }
    #mud-hint { height: 1; padding: 0 1; color: $text-muted; }
    #mud-scroll { height: 1fr; }
    #mud { padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        """Build the hint line and the scrollable view."""
        with Vertical(id="mud-box"):
            yield Label(
                "esc closes — type commands in the chat bar",
                id="mud-hint",
            )
            with VerticalScroll(id="mud-scroll"):
                yield Static(
                    "MUD output appears here. Type moves in the chat bar.",
                    id="mud",
                    markup=False,
                )

    def on_mount(self):
        """Repaint the last rendered view (see DoomScreen.on_mount)."""
        self.app._mud.refresh_pane()

    def action_dismiss(self):
        self.app.pop_screen()


class _ModuleList(VerticalScroll):
    """Container for module checkboxes that uses arrow keys to move focus."""

    def on_key(self, event):
        if event.key not in ("up", "down"):
            return
        event.stop()
        event.prevent_default()
        checkboxes = list(self.query(Checkbox))
        if not checkboxes:
            return
        focused = self.app.focused
        try:
            idx = checkboxes.index(focused)
        except ValueError:
            idx = -1 if event.key == "down" else 0
        if event.key == "up":
            idx = max(0, idx - 1)
        else:
            idx = min(len(checkboxes) - 1, idx + 1)
        cb = checkboxes[idx]
        cb.focus()
        screen = self.screen
        if isinstance(screen, ModulesScreen):
            screen._show_detail(cb.id.split("-", 1)[1])


class ModulesScreen(ModalScreen):
    """Enable or disable discovered Lua modules and reload the organism's
    module set. Space/enter toggles the focused checkbox; s saves & reloads;
    esc closes."""

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
        """Build the title, checkbox list, and detail label."""
        with Vertical(id="modules-box"):
            yield Label(
                "Modules — space toggles · s saves · esc closes",
                id="modules-title",
            )
            with _ModuleList(id="module-list"):
                pass
            yield Label("", id="module-detail")

    def on_mount(self):
        """Populate the checkbox list once the DOM is ready and focus it."""
        self._refresh_list()
        self.set_timer(0.05, self._ensure_focus)

    def _ensure_focus(self):
        container = self.query_one("#module-list", _ModuleList)
        checkboxes = list(container.query(Checkbox))
        if checkboxes:
            checkboxes[0].focus()
            self._show_detail(checkboxes[0].id.split("-", 1)[1])

    def _discovered(self):
        """Return discovered manifests sorted by name."""
        return sorted(self._loader._discover(), key=lambda m: m.get("name", ""))

    def _refresh_list(self):
        """Rebuild the checkbox list with current state."""
        container = self.query_one("#module-list", _ModuleList)
        container.remove_children()
        for manifest in self._discovered():
            name = manifest.get("name", "")
            # name only: the focused module's description renders in the
            # detail pane below, so the list row stays uncluttered
            cb = Checkbox(name, value=name in self._enabled, id=f"mod-{name}")
            container.mount(cb)
        checkboxes = list(container.query(Checkbox))
        if checkboxes:
            checkboxes[0].focus()

    def on_checkbox_changed(self, event):
        """A module checkbox was toggled: update the enabled set."""
        checkbox = event.checkbox
        if checkbox.id is None:
            return
        name = checkbox.id.split("-", 1)[1]
        if event.value:
            self._enabled.add(name)
            state = "enabled"
        else:
            self._enabled.discard(name)
            state = "disabled"
        self._show_detail(name)
        self.notify(f"{name} {state}")

    def _show_detail(self, name):
        manifest = next((m for m in self._discovered() if m.get("name") == name), {})
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
        # Make sure the loader uses the same enabled set before reloading.
        self._loader.modules_config = cfg["modules"]
        host = getattr(self._loader, "_host", None)
        if host is not None:
            # Hosted: reload on a FRESH bus — loader.load_all() alone would
            # re-subscribe module handlers on the stable bus (dispatch
            # duplicates) and strand the registry/persona references.
            host.reload_modules(
                modules_config=self._loader.modules_config,
                persona_config=self._loader.persona_config,
            )
        else:
            self._loader.load_all()
        self.notify(f"modules saved: {', '.join(sorted(self._enabled))}")
        self.dismiss(True)

    def action_dismiss(self, result=None):
        self.dismiss(result if result is not None else False)


# role -> log style (Rich markup); engine events get their own styles
# (STYLE_USER, STYLE_ORG, STYLE_DIM, STYLE_DREAM, STYLE_LEARNED, STYLE_SELF and
# STYLE_WARN are imported from tui_views.py so view styling lives in one place.)

NARRATE_INTERVAL = 45.0  # seconds between self-narrations (each = 5 LLM calls)
VOICE_PROBE_INTERVAL = 60.0  # seconds between ollama reachability probes
ASK_USER_ODDS = 0.35  # chance an idle wake utterance asks the user instead


class OrganismApp(App):
    """Replicanta's terminal front-end, conversation-first: a workspace
    chrome with a top organism bar and a nursery sidebar around a
    transcript-only main area; the mind/memory/inner/cells dashboards and
    the doom/mud games live in full-screen overlays (F3, /doom, /mud).
    Commands: /chaos N, /focus X, /sleep, /wake, /revive, /stats, /save,
    /think, /new, /swap, /organisms, /reload, /lua file.lua, /listen,
    /microphone, /look, /camera, /help (or ctrl+p / F1)."""

    TITLE = "Replicanta"

    COMMANDS: ClassVar[set] = App.COMMANDS | {SlashCommands}
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+p", "command_palette", "command palette"),
        Binding("f1", "help", "help"),
        Binding("f3", "being", "being"),
        Binding("ctrl+s", "save_now", "save"),
        Binding("ctrl+t", "think_now", "think"),
        Binding("f5", "talk", "talk (push-to-talk)"),
        Binding("f6", "look", "look through the camera"),
        Binding("f9", "modules", "modules"),
        Binding("ctrl+b", "toggle_sidebar", show=False),
        Binding("up", "doom_up", "doom forward", show=False),
        Binding("down", "doom_down", "doom back", show=False),
        Binding("left", "doom_left", "doom turn left", show=False),
        Binding("right", "doom_right", "doom turn right", show=False),
        Binding("space", "doom_shoot", "doom shoot", show=False),
        Binding("escape", "doom_stop", "doom stop", show=False),
        Binding("ctrl+q", "quit", "quit"),
        Binding("f10", "confirm_quit", "quit"),
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
    #sidebar-list { padding: 0; height: 1fr; border: none;
                     background: $surface; }
    #sidebar-list > ListItem { padding: 0 1; }
    #sidebar-list > ListItem.-highlight { background: $primary;
                                           color: $text; }
    #content { width: 1fr; height: 1fr; }
    #dreams { height: 1fr; padding: 0 1; }
    #pending { height: auto; max-height: 4; padding: 0 1; color: $success; }
    #mutation-banner { height: auto; display: none; padding: 0 1;
                       background: $warning-darken-2; color: $text; }
    #mutation-banner > Static { width: 1fr; content-align: left middle; }
    #mutation-banner > Button { min-width: 8; margin: 0 1; }
    #chat { height: 3; border: solid yellow; }
    #toast { height: auto; display: none; padding: 0 1;
             background: $error-darken-2; color: $text; }
    #help { border: round green; padding: 1 2; width: 88; height: auto;
            max-height: 90%; }
    OrganismMenuScreen, RenameScreen, NamePromptScreen, GroupMenuScreen,
    GroupPickScreen { align: center middle; }
    #org-menu, #group-menu, #group-pick { border: round $primary; width: 44;
                height: auto; background: $surface; }
    #rename-input, #name-input { border: round $primary; width: 60; }
    CellDetailScreen { align: center middle; }
    #cell-detail { border: round $secondary; padding: 1 2; width: 64;
                  height: auto; background: $surface; }
    CommandPalette { align: center middle; }
    #palette { width: 72; height: auto; max-height: 26; border: round $primary;
               background: $surface; padding: 1 2; }
    #palette-title { height: 1; padding: 0 1; text-style: bold;
                     color: $text; }
    #palette-results { height: auto; max-height: 18; border: none;
                       background: $surface; margin-top: 1; }
    #palette-results > ListItem { padding: 0 1; height: auto; }
    #palette-results > ListItem > Vertical { height: auto; }
    #palette-results > ListItem.-highlight { background: $primary; color: $text; }
    #palette-meta { height: 1; padding: 0 1; color: $text-muted; }
    .palette-usage { text-style: bold; }
    .palette-desc { color: $text-muted; }
    ModulesScreen { align: center middle; }
    #modules-box { width: 78; height: 24; border: round $primary; background: $surface; padding: 0 1; }
    #modules-title { width: 100%; height: 1; padding: 0 1; background: $surface; color: $text; text-style: bold; }
    #module-list { width: 100%; height: 1fr; min-height: 12; border: none; background: $surface; padding: 0; }
    #module-detail { width: 100%; height: auto; min-height: 4; padding: 1 2; color: $text-muted; border-top: solid $primary; }
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
        # metadata grid behind the cells section (BeingScreen), for
        # click-to-inspect
        self._cells_grid = []
        # transient activity message (shown in the #pending region)
        self._activity_text = ""
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
        self._completion_matches = None
        self._completion_index = 0
        self._chat_history = []
        self._history_index = -1
        self._history_draft = ""
        self._suppress_changed = False
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
        # per-subsystem behavior owners (tui_controllers.py): the app keeps
        # composition, binding wiring, rendering, and the @work boundaries
        self._mud = MudController(self)
        self._doom = DoomController(self)
        self._voice = VoiceController(self)
        self._brain_running = False  # cached fly-brain state for the activity line
        self._quit_hint_time = 0.0
        self._mind_text = ""
        self._memory_text = ""
        self._visual_text = ""
        self._topbar_text = ""
        self._rendered_topbar_text = None

    def compose(self) -> ComposeResult:
        """Build the top bar, sidebar, transcript, and chat input — the
        only chrome is the single top-bar row."""
        yield Static("", id="topbar")
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Static("nursery", id="sidebar-header")
                yield ListView(id="sidebar-list")
            with Vertical(id="content"):
                dreams = RichLog(
                    id="dreams",
                    max_lines=1000,
                    wrap=True,
                    markup=True,
                    highlight=False,
                    auto_scroll=True,
                )
                dreams.can_focus = False
                yield dreams
                yield Static("", id="pending", markup=False)
        yield MutationBanner(id="mutation-banner")
        self.chat_input = Input(
            placeholder="talk to me, or /help …  (tab completes)",
            id="chat",
        )
        yield self.chat_input
        yield Toast("", id="toast")

    def on_mount(self):
        """Render the initial organism state and start background timers."""
        # Mouse reporting starts ON: menus, buttons, and drag-and-drop are
        # click-driven. Press ctrl+m to turn it off when native terminal
        # text selection/copy is needed.
        self._mouse_enabled = True
        driver = getattr(self, "_driver", None)
        if driver is not None:
            # a headless driver may not implement mouse protocols at all
            with contextlib.suppress(Exception):
                driver._enable_mouse_support()
        self._show_org()
        if self.screen.size.width < 80:
            # narrow terminals start transcript-first; ctrl+b reveals the
            # nursery sidebar
            sidebar = self._safe_query("#sidebar", Vertical)
            if sidebar is not None:
                sidebar.styles.display = "none"
        self.set_interval(1.0, self._on_tick)
        self.set_interval(NARRATE_INTERVAL, self._maybe_narrate)
        self.set_interval(VOICE_PROBE_INTERVAL, self._probe_voice)
        self._probe_voice()
        self._maybe_narrate()
        # start with the cursor in the chat line, not the scrollable log
        self.chat_input.focus()

    def _show_org(self):
        """(Re)render everything that reflects the current organism: chat
        history, the top bar, and the dashboard sections. Used on mount
        and after a swap."""
        self._chat_history = [line for role, line in self.org.store.chat_log if role == "user"]
        if not self.org.store.chat_log and self.org.store.cycle == 0:
            self._append_log("a tiny replicanta wakes up inside your machine.", STYLE_DIM)
            self._append_log(
                "talk to it — it learns from you. /help (or F1) for commands. "
                "mouse capture is on; ctrl+m toggles it off to select/copy text.",
                STYLE_DIM,
            )
        for role, line in self.org.store.chat_log[-100:]:
            self._log_chat(role, line, stamp=False)
        self.org.hooks.set_emit(lambda msg: self._append_log(f"lua · {msg}", STYLE_DIM, stamp=True))
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
        if self._group is not None:
            # the group keeps the old (closed) organism object and would
            # keep broadcasting against unpersisted state — end the
            # session instead of re-seating it on the new organism
            names = ", ".join(self._group.names())
            for member in self._group.members.values():
                if member is not self.org:
                    member.flush(force=True)
            self._group = None
            group_ended = names
        else:
            group_ended = None
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
        if group_ended is not None:
            self._append_log(f"— group chat ended ({group_ended}) —", STYLE_DIM, stamp=True)
        self._append_log(f"— now living with {name} —", STYLE_DIM, stamp=True)

    # -- actions ---------------------------------------------------------
    def action_being(self):
        """F3: full-screen mind/memory/inner/cells dashboard overlay."""
        self.push_screen(BeingScreen(self.org))

    def action_toggle_sidebar(self):
        """ctrl+b: show/hide the nursery sidebar (auto-hidden on narrow
        terminals at mount)."""
        sidebar = self._safe_query("#sidebar", Vertical)
        if sidebar is None:
            return
        sidebar.styles.display = "none" if sidebar.styles.display != "none" else "block"

    def action_doom_up(self):
        self._doom.key_command("w")

    def action_doom_down(self):
        self._doom.key_command("s")

    def action_doom_left(self):
        # left arrow TURNS left — the port's own binding (strafe is q/e)
        self._doom.key_command("a")

    def action_doom_right(self):
        # right arrow TURNS right — the port's own binding (strafe is q/e)
        self._doom.key_command("d")

    def action_doom_shoot(self):
        self._doom.key_command("shoot")

    def action_doom_stop(self):
        self._doom.key_command("stop")

    def on_button_pressed(self, event):
        """Route mutation banner button presses."""
        button_id = event.button.id
        if button_id == "mutation-approve":
            entry = extensions.approve(self.org.dir_path / "artifacts" / "extensions.json")
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
            entry = extensions.reject(self.org.dir_path / "artifacts" / "extensions.json")
            if entry:
                self.org.store.remember("skill", f"patch rejected ({entry['kind']})")
                self._append_log(f"patch rejected ({entry['kind']})", STYLE_DIM, stamp=True)
            self._update_mutation_banner()
            return
        if button_id == "mutation-why":
            self._show_mutation_why()
            return

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

    def action_confirm_quit(self):
        """F10 asks before quitting, unlike ctrl+q which is the fast path."""

        def check(answer):
            if answer:
                self.action_quit()

        class QuitDialog(ModalScreen[bool]):
            BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "dismiss", "close")]
            CSS = """
            QuitDialog { align: center middle; }
            #quit-box { width: 50; height: auto; border: round $primary; padding: 1 2; background: $surface; }
            """

            def compose(self) -> ComposeResult:
                with Vertical(id="quit-box"):
                    yield Static("Quit Replicanta?", classes="title")
                    yield Static("The organism state will be saved.")
                    yield Horizontal(
                        Button("Yes", id="quit-yes", variant="primary"),
                        Button("No", id="quit-no"),
                    )

            def on_button_pressed(self, event):
                self.dismiss(event.button.id == "quit-yes")

            def on_key(self, event):
                if event.key == "y":
                    self.dismiss(True)
                elif event.key == "n":
                    self.dismiss(False)

        self.push_screen(QuitDialog(), callback=check)

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
        # mkstemp: unpredictable name, mode 0600 — the chat log must not be
        # world-readable in a shared /tmp.
        fd, tmp = tempfile.mkstemp(prefix="replicanta-chat-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        path = Path(tmp)
        self._append_log(f"— chat log saved to {path} —", STYLE_DIM, stamp=True)

    def _export_chat(self, path=None):
        """Write the full chat log to a markdown file. Returns the path.

        Exports are confined to ``~/.replicanta/exports/`` and the optional
        argument is a bare filename (``safe_name``), never a path — /export
        must not become an arbitrary file write.
        """
        from datetime import datetime

        org_name = self._org_name()
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        export_dir = Path.home() / ".replicanta" / "exports"
        if path:
            name = fileutil.safe_name(path)
            if not name.endswith(".md"):
                name += ".md"
        else:
            name = f"replicanta-chat-{org_name}-{timestamp}.md"
        dest = fileutil.safe_path(export_dir, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fileutil.atomic_write_text(dest, fileutil.render_chat_export(org_name, self.org.store), root=export_dir)
        return dest

    def on_text_selected(self, event) -> None:
        """Terminal habit, made real: releasing a drag copies the selection.

        Textual highlights a selection but never copies it by itself — the
        copy_text action must be invoked (Screen binds it to ctrl+c, which
        this app reserves for quitting, and ctrl+shift+c is copy_chat). With
        terminal mouse reporting on — required for clicks, submenu boxes,
        and sidebar drags — the terminal's own selection is unreachable, so
        auto-copy here. Delivery is OSC52, which works on most terminals
        (kitty, wezterm, alacritty, foot, tmux with set-clipboard)."""
        with contextlib.suppress(SkipAction):
            self.screen.action_copy_text()

    def action_toggle_mouse(self):
        """Toggle Textual's terminal mouse reporting. When enabled, mouse clicks
        work inside the app; when disabled, the terminal regains native text
        selection. We track the desired state in an instance flag because
        Textual's capture_mouse API is for widget-level capture, not for
        turning the terminal's mouse protocol on/off."""
        self._mouse_enabled = getattr(self, "_mouse_enabled", True)
        self._mouse_enabled = not self._mouse_enabled
        driver = getattr(self, "_driver", None)
        if driver is not None:
            # mouse protocol support varies by driver/terminal; the toggle
            # state is tracked in _mouse_enabled either way
            with contextlib.suppress(Exception):
                if self._mouse_enabled:
                    driver._enable_mouse_support()
                else:
                    driver._disable_mouse_support()
        status = "enabled" if self._mouse_enabled else "disabled"
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
        """Render the custom top bar: wordmark and organism identity on the
        left, mood and mental state in the center, voice/mic/clock indicators
        on the right — one terminal line, aiperf-style."""
        lc = self.org.lifecycle
        word = {"wake": "awake", "sleep": "asleep", "dead": "faded"}.get(lc.state, lc.state)
        state_style = {"wake": "green", "sleep": "cyan", "dead": "red"}.get(lc.state, "")
        mood = self.org.store.belief_value("self", "mood", "calm")
        s = self.org.store
        voice_state = voice.status()
        voice_style = {"online": "green", "offline": "red"}.get(voice_state, "dim")
        recording = self.listener.recording
        spoken = speech.enabled
        clock = self.org.probe.clock_utc()
        left = Text.assemble(
            ("◆ REPLICANTA", "bold cyan"),
            ("  │  ", "dim"),
            (self._org_name(), "bold"),
            (self._module_badges(), ""),
            ("  ·  ", "dim"),
            (word, state_style),
            ("  ·  ", "dim"),
            (mood, "dim"),
        )
        center = Text.assemble(
            ("a/c/i ", "dim"),
            (f"{s.arousal:.2f}/{s.coherence:.2f}/{s.incoherence:.2f}", "bold"),
        )
        right = Text.assemble(
            ("voice ", "dim"),
            (voice_state, voice_style),
        )
        if recording:
            right.append("   ")
            right.append(f"mic {self._recording_elapsed()}", style="reverse green")
        if spoken:
            right.append("   ")
            # yellow when enabled but the voice extras are missing — the
            # indicator must not claim speech that cannot sound
            spk_style = "reverse green" if speech.ready() else "reverse yellow"
            right.append("spk", style=spk_style)
        right.append("   ")
        right.append(clock, style="bold")
        bar = Table.grid(expand=True)
        bar.add_column(justify="left")
        bar.add_column(justify="center")
        bar.add_column(justify="right")
        bar.add_row(left, center, right)
        mic = f" mic {self._recording_elapsed()}" if recording else ""
        text = (
            f"◆ REPLICANTA │ {self._org_name()}{self._module_badges()} · {word} · {mood} · "
            f"a/c/i {s.arousal:.2f}/{s.coherence:.2f}/{s.incoherence:.2f} · "
            f"voice {voice}{mic}{' spk' if spoken else ''} · {clock}"
        )
        self._topbar_text = text
        if text == self._rendered_topbar_text:
            return
        self._rendered_topbar_text = text
        topbar = self._safe_query("#topbar", Static)
        if topbar is not None:
            topbar.update(bar)

    def _module_badges(self):
        """Glyph suffix marking the loaded capability modules (e.g. ' 🪰'),
        so every entity in the nursery shows what it has activated."""
        loader = getattr(self.org, "module_loader", None)
        if loader is None:
            return ""
        return "".join(f" {glyph}" for name, glyph in MODULE_BADGES.items() if name in loader.modules)

    def _badges_for_organism(self, name):
        """Capability badges for one sidebar row. The app only holds the
        current organism's module loader, so other organisms' badges come
        from their own config file (same source the loader reads)."""
        if name == self.org.dir_path.name:
            return self._module_badges()
        try:
            cfg_path = nursery.organism_dir(self.root, name) / "replicanta.toml"
            enabled = tomllib.loads(cfg_path.read_text()).get("modules", {}).get("enabled", [])
        except Exception:  # noqa: BLE001 — a row without readable config just shows no badges
            return ""
        enabled = {"doom-ascii" if m == "nano-doom" else m for m in enabled}
        return "".join(f" {glyph}" for mod, glyph in MODULE_BADGES.items() if mod in enabled)

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
            lv.append(ListItem(Label(f"{marker}{name}{self._badges_for_organism(name)}"), name=name))
        for gname in sorted(groups):
            lv.append(ListItem(Label(f"▾ {gname}"), name=f"group:{gname}"))
            for member in groups[gname]:
                marker = "● " if member == current else "  "
                lv.append(ListItem(Label(f"   {marker}{member}{self._badges_for_organism(member)}"), name=member))

    def on_list_view_selected(self, event):
        """Sidebar selection (left click or Enter) swaps to the organism
        immediately — no confirmation menu. Rename / move-to-group / cancel
        live on the right-click menu."""
        if not event.item.name:
            return
        # a right-click opens the context menu (on_mouse_down); the ListView
        # still posts Selected for it, which must not ALSO swap
        if self._last_click_button == 3:
            self._last_click_button = None
            return
        if event.item.name.startswith("group:"):
            self._open_group_menu(event.item.name[6:])
        else:
            self._swap_to_sidebar(event.item.name)

    def _swap_to_sidebar(self, name):
        """Swap straight to a sidebar organism. Clicking the current one is
        a no-op; stale names (deleted out from under the list) warn."""
        if name == self.org.dir_path.name:
            return
        if name not in nursery.list_organisms(self.root):
            self._append_log(f"sidebar: no organism {name!r}", STYLE_WARN)
            return
        self._swap_to(name)

    def _sidebar_item_at(self, screen_x, screen_y):
        """(item, in_sidebar) for a screen position: the sidebar ListItem
        under it (None over chrome/empty space); in_sidebar is False when
        the position is outside the sidebar entirely."""
        try:
            widget, _region = self.screen.get_widget_at(screen_x, screen_y)
        except Exception:  # noqa: BLE001 — NoWidget when clicking in a screen gap
            return None, False
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
        group (a plain click never moves far enough to become one) — and a
        plain left click swaps to that organism. Right-click in the sidebar:
        an organism opens its action menu (swap / rename / move-to-group),
        a group header its rename prompt, empty space the new-group prompt.
        (Textual's Click message is left-button only, so button 3 is
        handled directly here.)"""
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
        """Left-click a neural-memory cell (CELLS section of the being
        overlay) to inspect what kind of object it holds and its metadata.
        Grid geometry: one header line, then CELLS_ROWS lines of
        2-column-wide cells."""
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

        self.push_screen(OrganismMenuScreen(name, name == self.org.dir_path.name), on_choice)

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

        self.push_screen(NamePromptScreen("new group name (letters, digits, spaces, - _ .)"), on_name)

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
                self._prompt_new_group(on_created=lambda name: self._assign_to_group(org_name, name))
                return
            self._assign_to_group(org_name, choice or None)

        self.push_screen(GroupPickScreen(org_name, nursery.list_groups(self.root)), on_pick)

    def action_think_now(self):
        self._maybe_narrate()

    def action_talk(self):
        self._toggle_listen()

    def action_look(self):
        self._look_now()

    def action_sleep_wake(self):
        """Toggle between wake and sleep states."""
        if self.org.lifecycle.state == "wake":
            self.dispatch_command("/sleep")
        else:
            self.dispatch_command("/wake")

    def action_voice(self):
        """Toggle spoken voice output."""
        self.dispatch_command("/voice")

    def action_mud(self):
        """Toggle the MUD mini-game."""
        self._mud.command([])

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
            sight = voice.describe_image(frame)
        except Exception as exc:  # noqa: BLE001 — vision model offline etc.
            self.call_from_thread(self._append_log, f"sight failed: {exc}", STYLE_WARN)
            return
        self.call_from_thread(self._set_sight, sight)

    # -- mud (worker boundaries; behavior in MudController) ------------------
    @work(thread=True)
    def _mud_turn(self):
        self._mud.turn()

    @work(thread=True)
    def _mud_scenario_worker(self, description):
        self._mud.scenario_worker(description)

    def _set_sight(self, sight):
        self.org.see(sight)
        self._append_log(f"the organism sees: {sight}", STYLE_LEARNED, stamp=True)

    def _camera(self, args):
        """Manage the eye: bare = status, list = devices, use <dev> = pick
        one (index, /dev/videoN, or name substring)."""
        if not args:
            self._append_log(
                f"camera: /dev/video{self.camera.device} · vision model "
                f"{voice.vision_model()} · /camera list · "
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
        and transcribes. The transcript lands in the chat line — editable,
        reviewed, sent with Enter like anything typed."""
        if self.listener.recording:
            elapsed = self._recording_elapsed()
            audio = self.listener.stop()
            self._append_log(f"transcribing ({elapsed})…", STYLE_DIM)
            self._transcribe_then_say(audio)
        else:
            self.listener.start()
            if self.listener.recording:
                self._recording_started = time.monotonic()
                self._append_log("listening… (F5 or /listen to stop · Esc to cancel)", STYLE_DIM)
                # load the STT model in the background so the stop press
                # transcribes immediately instead of paying model load
                threading.Thread(target=self.listener.warmup, daemon=True, name="stt-warmup").start()
            else:
                self._append_log("no microphone (device missing or busy?)", STYLE_WARN)
        self.refresh_status()
        self.refresh_top_bar()

    def _recording_elapsed(self):
        """mm:ss since recording started ('0:00' when not recording)."""
        started = getattr(self, "_recording_started", None)
        if started is None or not self.listener.recording:
            return "0:00"
        seconds = int(time.monotonic() - started)
        return f"{seconds // 60}:{seconds % 60:02d}"

    def cancel_listen(self):
        """Discard the in-progress recording without transcribing it."""
        if not self.listener.recording:
            return False
        self.listener.stop()
        self._recording_started = None
        self._append_log("listening cancelled", STYLE_DIM)
        self.refresh_status()
        self.refresh_top_bar()
        return True

    @work(thread=True)
    def _transcribe_then_say(self, audio):
        text = self.listener.transcribe(audio)
        self.call_from_thread(self._deliver_transcript, text)

    def _deliver_transcript(self, text):
        """UI-thread delivery of a transcript: the text lands in the chat
        line (editable, reviewed) and Enter sends it like a typed line;
        nothing is auto-submitted."""
        if text:
            self._set_chat_value(text)
            if self.chat_input is not None:
                self.chat_input.focus()
            self._append_log(f"heard: {text}", STYLE_DIM)
        else:
            self._append_log("(heard nothing)", STYLE_DIM)
        self._recording_stopped()

    def _recording_stopped(self):
        self._recording_started = None
        self.refresh_status()
        self.refresh_top_bar()

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
        if event.key == "escape" and self.listener.recording:
            # recording cancel wins over every other Escape meaning: the
            # user is actively holding the mic and wants out without
            # transcribing or sending anything
            self.cancel_listen()
            event.prevent_default()
            event.stop()
            return
        if event.key == "tab":
            if isinstance(self.screen, DoomScreen):
                # The overlay has its own chat line: the main input lives on
                # the screen beneath the overlay and can't be seen while the
                # game runs. Focus the overlay input for typing; Enter hands
                # focus back to the game (DoomScreen.focus_game).
                if self.focused is not self.screen.query_one("#doom-chat", Input):
                    self.screen.query_one("#doom-chat", Input).focus()
                else:
                    self.screen.focus_game()
                event.prevent_default()
                event.stop()
                return
            if self.chat_input is None:
                return
            if not self.chat_input.has_focus:
                # Tab lands in the chat line: never let Textual's default
                # focus cycling strand typing on an invisible widget.
                self.chat_input.focus()
            else:
                value = self.chat_input.value
                if self._completion_matches is None:
                    # capture the candidates once per typed token, then
                    # cycle that same list across consecutive Tabs —
                    # recomputing from the just-completed word would
                    # collapse the match set to one
                    self._completion_matches = tui_commands.completion_matches(value)
                    self._completion_index = 0
                if self._completion_matches:
                    new_value, self._completion_index = tui_commands.complete_command(
                        value,
                        self._completion_matches,
                        self._completion_index,
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
            if isinstance(self.screen, DoomScreen):
                # on the DOOM overlay the arrows drive the player (doom_up/
                # doom_down bindings); prevent_default here would swallow
                # them, so leave the key to binding dispatch
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
        if event.input.id != "chat":
            # rename/name prompts and the palette input also bubble
            # Changed up to the app; only the chat line drives typing state
            return
        if not self._suppress_changed:
            self._completion_matches = None
            self._completion_index = 0
            self._history_index = -1
        self._suppress_changed = False
        if event.value and not event.value.startswith("/"):
            self._touch_typing()

    def _touch_typing(self):
        """Record typing activity with debouncing; nudges near-boundary sleep."""
        now = time.monotonic()
        self._typing_last = now
        self.set_activity("listening…")
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
        self.clear_activity()
        self._typing_timer = None

    # -- voice health (worker boundary; behavior in VoiceController) --------
    def _probe_voice(self):
        self._voice.probe_due()

    @work(thread=True)
    def _probe_voice_worker(self):
        self._voice.probe()

    # -- spoken voice (piper tts; behavior in VoiceController) --------------
    @work(thread=True)
    def _voice_download(self, name):
        self._voice.download(name)

    # -- ticks -----------------------------------------------------------
    def _on_tick(self):
        for event in self.org.tick(1.0):
            self._render_event(event)
        if self._busy():
            self._busy_frame = (self._busy_frame + 1) % 3
        self._update_brain_activity()
        self._refresh_views()
        self.refresh_top_bar()
        self.refresh_status()

    def _update_brain_activity(self):
        """Show/hide the transient fly-brain activity indicator. Use U+1FAB0 🪰
        when the brain is processing so the activation state is visually
        distinct from the static ready indicator in the status bar."""
        loader = getattr(self.org, "module_loader", None)
        svc = loader.registry.get("brain") if loader is not None else None
        running = svc is not None and svc.running()
        if running == self._brain_running:
            return
        self._brain_running = running
        if running:
            self.set_activity("\U0001fab0 fly brain processing data…")
        else:
            self.clear_activity()

    def _busy(self):
        return self._narrating or self._responding or self._self_talking or self._group_responding

    def _refresh_views(self):
        """Rebuild the cheap string views and, when the dashboard overlay is
        up, its live sections; refresh the game overlays and banner."""
        self._mind_text = tui_views.mind_view(self.org)
        self._memory_text = tui_views.memory_view(self.org)
        self._cells_grid = tui_views.cells_layout(self.org)[1]
        try:
            screen = self.screen
        except ScreenStackError:
            screen = None  # headless dispatch: no screen mounted yet
        if isinstance(screen, BeingScreen):
            screen.refresh_sections(self.org)
        # Live refresh: if a visual chart is active and state changed, re-render silently.
        if getattr(self, "_visual_kind", None):
            sig = self._visual_signature()
            if sig != getattr(self, "_visual_sig", None):
                self._render_visual(self._visual_kind, log=False)
        self._doom.refresh()
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
                self._append_log(f"— the organism drifts to {to} —", STYLE_DIM, stamp=True)
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
            self._append_log(f"the host strains (distress +{event['distress']:.2f})", STYLE_WARN)
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
            self._append_log(f"goal completed: {event['text']}", STYLE_LEARNED, stamp=True)
            self.notify(f"goal completed: {event['text']}")
        elif kind == "want_reflect":
            self._reflect()

    def refresh_status(self):
        """Former bottom-bar updater. The single top bar is the only chrome
        row now (refresh_top_bar renders it); this remains as the no-op seam
        controllers and command handlers call after state changes."""

    def set_activity(self, text):
        """Show a transient activity message in the #pending region above
        the chat line (also mirrored into the doom overlay's thought
        stream by its controller)."""
        self._activity_text = text
        pending = self._safe_query("#pending", Static)
        if pending is not None:
            pending.update(text)

    def clear_activity(self):
        """Hide the transient activity message, unless the #pending region
        has moved on to other content (streamed reply tokens, doom
        thoughts) — those own the region until their own hide."""
        text, self._activity_text = self._activity_text, ""
        if not text:
            return
        pending = self._safe_query("#pending", Static)
        if pending is not None and str(getattr(pending, "_Static__content", "") or "") == text:
            pending.update("")

    @property
    def activity_text(self):
        """Current activity message (for tests)."""
        return self._activity_text

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
            return True
        return False

    def _append_log_lines(self, text, style=None, stamp=False):
        """Append multiple lines, returning whether any line was written."""
        written = False
        for line in (text or "").splitlines():
            if self._append_log(line, style=style, stamp=stamp):
                written = True
        return written

    def _org_name(self):
        """Card title for the organism: its learned name (the user can give
        it one with 'your name is …'), else its nursery dir name."""
        return self.org.store.belief_value("self", "name", self.org.dir_path.name or "replicanta")

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
        self._append_log("diary: entry saved (artifacts/diary.md)", STYLE_DIM, stamp=True)
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
                body = f"{detail}\nwhy: {entry.get('why', '')}\nauto-apply is on; toggle with /auto-apply off"
            else:
                body = f"{detail}\nwhy: {entry.get('why', '')}\n/approve to accept · /reject to discard"
            self._write_card(f"{self._org_name()} · proposes a patch", body, "yellow")
            self.notify(
                "patch proposed — /approve or /reject" if not auto else "patch applied automatically",
                severity="warning" if not auto else "information",
            )
            self.refresh_status()
            return
        self.org.store.remember("skill", f"{result['action']} skill: {result['name']}")
        self._append_log(f"skill {result['action']}: {result['name']}", STYLE_LEARNED, stamp=True)
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
        if self.org.lifecycle.state == "wake" and not self._last_was_question and self._rng.random() < ASK_USER_ODDS:
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
        if event.input.id == "doom-chat":
            # The DOOM overlay's own chat line: same routing as the main
            # input (slash commands, moves, conversation), plus an echo in
            # the overlay's thought stream, since the main chat log sits on
            # the screen beneath this one and can't be seen.
            text = event.value.strip()
            event.input.value = ""
            if text:
                tui_commands.history_push(self._chat_history, text)
                if isinstance(self.screen, DoomScreen):
                    self.screen.show_user_line(text)
                if text.startswith("/"):
                    self.dispatch_command(text)
                elif not self._doom.chat_command(text):
                    self.route_chat_message(text)
            if isinstance(self.screen, DoomScreen):
                self.screen.focus_game()
            return
        if event.input.id != "chat":
            # modal rename/name prompts and the palette input bubble
            # Submitted up to the app too; only the chat line is chat
            return
        text = event.value.strip()
        self.query_one("#chat", Input).value = ""
        tui_commands.history_push(self._chat_history, text)
        if text.startswith("/"):
            self.dispatch_command(text)
        elif text:
            self.route_chat_message(text)

    def dispatch_command(self, cmd):
        """Parse and dispatch a slash-command line from the chat input."""
        parts = cmd.split()
        name = parts[0]
        with telemetry.get_tracer(__name__).start_as_current_span("tui.command") as span:
            span.set_attribute("command", name)
            try:
                self._dispatch(name, parts)
            except (ValueError, IndexError) as exc:
                # a mistyped argument must never kill the input handler
                span.record_exception(exc)
                span.set_status(telemetry.Status(telemetry.StatusCode.ERROR))
                self._append_log(f"{name}: {exc}", STYLE_WARN)

    def _dispatch(self, name, parts):
        """Dispatch a parsed slash command: registry lookup first, then the
        module CommandService seam, then the unknown-command fallback."""
        handler = tui_commands.COMMAND_HANDLERS.get(name)
        if handler is not None:
            handler(self, parts)
            return
        self._dispatch_module_command(name, parts)

    def _dispatch_module_command(self, name, parts):
        """Fall through to module-registered commands; report unknowns.

        Any verb a Lua module registered with the CommandService works
        here without a native TUI branch."""
        loader = getattr(self.org, "module_loader", None)
        commands = loader.registry.get("commands") if loader is not None else None
        if commands is not None and commands.has(name):
            try:
                result = commands.dispatch(name, parts[1:])
            except Exception as exc:  # noqa: BLE001 — user command must not crash
                self._append_log(f"{name} failed: {exc}", STYLE_WARN)
                return
            self._append_log_lines(str(result or ""), STYLE_DIM)
            return
        self._append_log(f"unknown: {name} (try /help)", STYLE_WARN)
        self.show_toast(f"Invalid command: {name}")

    def _visualize_command(self, args):
        """Dispatch /visualize through the visual-state module's Lua-registered
        handler, then keep the live SVG chart re-rendering from the tick."""
        loader = getattr(self.org, "module_loader", None)
        if loader is None:
            self._append_log("module loader unavailable", STYLE_WARN)
            return
        commands = loader.registry.get("commands")
        if commands is None or not commands.has("/visualize"):
            self._append_log("visual-state module not loaded (enable it via /modules)", STYLE_WARN)
            return
        try:
            result = commands.dispatch("/visualize", args if args else [])
        except Exception as exc:  # noqa: BLE001 — user command must not crash
            self._append_log(f"visualize failed: {exc}", STYLE_WARN)
            return
        self._append_log_lines(str(result or ""), STYLE_DIM)
        # The Lua handler owns the verb and prints the text chart; a live
        # SVG chart is re-rendered silently from the tick while active.
        kind = args[0] if args else "summary"
        if kind in rdd.supported_kinds():
            self._render_visual(kind, log=False)

    def _hand_command(self, args):
        """Dispatch /hand through the tendon-hand module's Lua-registered
        handler."""
        loader = getattr(self.org, "module_loader", None)
        if loader is None:
            self._append_log("module loader unavailable", STYLE_WARN)
            return
        commands = loader.registry.get("commands")
        if commands is None or not commands.has("/hand"):
            self._append_log("tendon-hand module not loaded (enable it via /modules)", STYLE_WARN)
            return
        try:
            result = commands.dispatch("/hand", args if args else ["state"])
        except Exception as exc:  # noqa: BLE001 — user command must not crash
            self._append_log(f"hand command failed: {exc}", STYLE_WARN)
            return
        self._append_log_lines(str(result or ""), STYLE_DIM)

    def _brain_command(self, args):
        """Dispatch /brain through the fly-brain module's Lua-registered
        handler; the module owns the verb ladder."""
        loader = getattr(self.org, "module_loader", None)
        if loader is None:
            self._append_log("module loader unavailable", STYLE_WARN)
            return
        commands = loader.registry.get("commands")
        if commands is None or not commands.has("/brain"):
            self._append_log("fly-brain module not loaded (enable it via /modules)", STYLE_WARN)
            return
        try:
            result = commands.dispatch("/brain", args if args else [])
        except Exception as exc:  # noqa: BLE001 — user command must not crash
            self._append_log(f"brain command failed: {exc}", STYLE_WARN)
            return
        self._append_log_lines(str(result or ""), STYLE_DIM)
        # If the command started an async run, refresh status so the indicator appears.
        if args and args[0].lower() in ("run", "optimize", "adapt"):
            self.refresh_status()
            self.set_activity("🪰 fly brain is running…")

    def _render_visual(self, kind, log=False):
        """Render or re-render the active visual chart."""
        visual = getattr(self.org, "module_loader", None)
        if visual is None:
            if log:
                self._append_log("visual service unavailable", STYLE_WARN)
            return
        svc = visual.registry.get("visual")
        if svc is None:
            if log:
                self._append_log("visual-state module not loaded (enable it via /modules)", STYLE_WARN)
            return
        try:
            result = svc.build(kind)
        except Exception as exc:  # noqa: BLE001 — user command must not crash
            if log:
                self._append_log(f"visualize failed: {exc}", STYLE_WARN)
            return
        header = f"{result.kind} — {result.caption}"
        if log:
            self._append_log(f"visual state: {result.kind}", STYLE_DIM, stamp=True)
            self._append_log(f"saved: {result.path}", STYLE_DIM)
            for line in result.text_chart.splitlines():
                self._append_log(line, STYLE_DIM)
        self._visual_text = f"{header}\n\n{result.text_chart}"
        self._visual_kind = result.kind
        self._visual_sig = self._visual_signature()

    def _visual_signature(self):
        """Return a cheap signature of organism state for live chart refresh."""
        store = self.org.store
        beliefs = len(store.beliefs())
        memories = len(store.memory)
        activity = sum(v for v in store.activity.values() if isinstance(v, (int, float)))
        return (store.cycle, beliefs, memories, activity)

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
            line = "personas: " + ", ".join(f"*{n}" if active and active["name"] == n else n for n in names)
            self._append_log(line, STYLE_DIM)
        elif args[0] == "off":
            svc.deactivate()
            self._append_log("persona cleared", STYLE_DIM)
        else:
            svc.activate(args[0])
            # activate() returns None on both paths, so verify through
            # active() before announcing success.
            active = svc.active()
            if active is not None and active["name"] == args[0]:
                self._append_log(f"persona: {args[0]}", STYLE_DIM)
            else:
                self._append_log(f"unknown persona {args[0]!r} (try /persona list)", STYLE_WARN)

    def _modules_command(self, args):
        if args and args[0] != "manage":
            self._append_log("/modules [manage]", STYLE_DIM)
            return
        self.action_modules()

    def route_chat_message(self, text):
        """Route ordinary user chat to DOOM, MUD, group chat, or the organism."""
        self._log_chat("user", text)
        if self._doom.chat_command(text):
            return
        if self._mud.route_text(text):
            return
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

    def _maybe_respond(self, text, *, quick=False, temperature=None):
        if not self._responding:
            self._responding = True
            self.refresh_status()
            self._respond(text, quick=quick, temperature=temperature)

    @work(thread=True)
    def _respond(self, text, *, quick=False, temperature=None):
        org = self.org  # capture: a swap mid-debate drops the delivery
        reply = None
        self.call_from_thread(self.set_activity, "org is thinking")
        try:
            self.call_from_thread(self._pending_show, "org is thinking")
            reply = voice.respond(
                org,
                text,
                on_token=lambda tok: self.call_from_thread(self._pending_token, tok),
                quick=quick,
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "reply", exc)
        finally:
            self.call_from_thread(self.clear_activity)
            self._responding = False
        if reply is not None and org is self.org:
            # If the entity is in the middle of a doom game, show its thinking
            # in the chat log, execute any doom.command line, and refresh the
            # DOOM overlay so the user sees the result.
            doom_cmd = extract_doom_command(reply)
            if doom_cmd is not None and time.monotonic() < self._doom._manual_until:
                # The human is driving: an entity move buried in a chat
                # reply must not fire mid-play (the utterance hook is
                # Lua-gated; this is the Python-side execution path).
                doom_cmd = None
            if doom_cmd is not None:
                # Render the command itself in chat as a system line so the
                # user can analyze the entity's decision.
                self.call_from_thread(
                    self._append_log,
                    f'doom.command("{doom_cmd}")',
                    STYLE_SELF,
                    stamp=True,
                )
                self.call_from_thread(self._doom.command, [doom_cmd])
                with contextlib.suppress(Exception):
                    self.org.store.add(("doom", "last_action", doom_cmd), 0.7)
            # Mirror the entity's prose reasoning into the DOOM overlay when a game is running.
            self._doom.mirror_thought(reply)
            self.call_from_thread(self._set_reply, reply)
            # If a doom game is still running after the entity's move, queue
            # another turn so it keeps playing autonomously. The human can
            # interrupt by chatting or taking manual control.
            self.call_from_thread(self._doom.schedule_turn)

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
                    "no active group — /group start " + " ".join(nursery.list_organisms(self.root)),
                    STYLE_DIM,
                )
            else:
                self._append_log(
                    f"group chat: {', '.join(self._group.names())} ({len(self._group.transcript)} messages)",
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
            self._append_log(f"/group: unknown organisms or groups: {', '.join(missing)}", STYLE_WARN)
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

    telemetry.init_telemetry()
    # Send warnings and errors to a rotating log so we can diagnose TUI
    # behavior (especially background workers) without cluttering the terminal.
    log_path = Path.home() / ".local" / "share" / "replicanta" / "replicanta.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_path),
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Replicanta TUI")
    parser.add_argument("--dir", default=str(Path(__file__).parent))
    parser.add_argument("--org", default=None, help="organism name in the nursery")
    parser.add_argument("--wake", type=int, default=300)
    parser.add_argument("--sleep", type=int, default=60)
    parser.add_argument(
        "--bed-hour",
        type=int,
        default=23,
        help="local hour (0-23) the organism goes to sleep; -1 disables circadian scheduling",
    )
    parser.add_argument(
        "--rise-hour",
        type=int,
        default=7,
        help="local hour (0-23) the organism wakes for the day",
    )
    parser.add_argument("--chaos", type=float, default=0.5)
    parser.add_argument(
        "-web",
        "--web",
        action="store_true",
        help="launch the local Glasshouse web UI",
    )
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8765, help="Glasshouse port")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser with --web")
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
        "bed_hour": args.bed_hour if 0 <= args.bed_hour <= 23 else None,
        "rise_hour": args.rise_hour if 0 <= args.rise_hour <= 23 else None,
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
