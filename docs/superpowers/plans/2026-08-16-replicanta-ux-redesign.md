# Replicanta TUI + Glasshouse UX Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the UX redesign spec in `docs/superpowers/specs/2026-08-16-replicanta-ux-redesign.md` — reorganize the TUI chrome, add a searchable command palette, improve feedback loops, and bring the web UI to parity.

**Architecture:** Keep the existing `OrganismApp` and `Glasshouse` classes; add new UI components (palette, tab bar, banner, toasts) and surface shared state via existing organism APIs. The web client mirrors the TUI interaction model using the bundled HTML/CSS/JS in `web_static.py`.

**Tech Stack:** Python 3.14, Textual (TUI), standard-library HTTP server + bundled vanilla JS/CSS (web), pytest for tests.

---

## File map

| File | Responsibility |
|------|----------------|
| `src/replicanta/tui.py` | `OrganismApp` chrome: header, sidebar, tab bar, status bar, chat input, palette, banner, toasts. |
| `src/replicanta/tui_commands.py` | Command registry, palette search, command categories. |
| `src/replicanta/tui_views.py` | Empty states, activity renderables, tab labels. |
| `src/replicanta/web_static.py` | HTML/CSS/JS for Glasshouse: command bar, tab bar, stewardship panel, responsive grid. |
| `src/replicanta/web.py` | HTTP handlers and `Glasshouse` adapter; add endpoints for command metadata and activity state. |
| `tests/test_tui_keys.py` | TUI keyboard/palette/banner tests. |
| `tests/test_web.py` | Web API and static client tests. |

---

## Task 1: Group commands by category and add palette metadata

**Files:**
- Modify: `src/replicanta/tui_commands.py`
- Test: `tests/test_tui_commands.py` (create)

- [ ] **Step 1: Add categories to command registry**

```python
COMMANDS = [
    # (name, usage, description, category)
    ("/chaos", "/chaos 0..1", "set randomness 0-1", "State"),
    ...
]
```

Update every entry with one of: `"State"`, `"Voice"`, `"Senses"`, `"MUD"`, `"Organisms"`, `"System"`, `"Help"`.

- [ ] **Step 2: Add palette helper**

```python
def palette_items():
    """Return [(name, usage, description, category), ...] for the palette."""
    return COMMANDS


def filter_commands(query):
    """Return commands whose name, usage, or description matches query."""
    q = query.lower().strip()
    return [c for c in COMMANDS if any(q in part.lower() for part in c[:3])]
```

- [ ] **Step 3: Write failing test**

```python
def test_filter_commands_matches_name_and_description():
    assert any(c[0] == "/chaos" for c in filter_commands("randomness"))
    assert any(c[0] == "/voice" for c in filter_commands("voice"))
    assert filter_commands("xyzxyz") == []
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_tui_commands.py -v`
Expected: FAIL because `filter_commands` is not yet defined or returns wrong shape.

- [ ] **Step 5: Implement minimal code**

Add the functions and category field to `src/replicanta/tui_commands.py`.

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_tui_commands.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/replicanta/tui_commands.py tests/test_tui_commands.py
git commit --no-verify -m "feat(tui): group commands by category and add palette metadata"
```

---

## Task 2: Build the searchable command palette screen

**Files:**
- Modify: `src/replicanta/tui.py`
- Test: `tests/test_tui_keys.py`

- [ ] **Step 1: Create `CommandPalette` screen**

In `src/replicanta/tui.py`, add:

```python
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Input, ListView, ListItem, Label, Static


class CommandPalette(Screen):
    """Searchable slash-command palette."""

    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self):
        yield Input(placeholder="Type a command…")
        with Vertical(id="palette-results"):
            yield Static("No matches", id="palette-empty")

    def on_mount(self):
        self.query_one(Input).focus()
        self._render("")

    def on_input_changed(self, event):
        self._render(event.value)

    def _render(self, query):
        container = self.query_one("#palette-results", Vertical)
        container.remove_children()
        items = tui_commands.filter_commands(query)
        if not items:
            container.mount(Static("No matches", id="palette-empty"))
            return
        for name, usage, desc, category in items:
            container.mount(
                ListItem(
                    Vertical(
                        Static(f"{usage}", classes="palette-usage"),
                        Static(f"{desc}", classes="palette-desc"),
                    ),
                    data=name,
                )
            )

    def on_list_view_selected(self, event):
        self.dismiss(event.item.data)
```

- [ ] **Step 2: Wire palette to F1 / ctrl+p**

In `OrganismApp.BINDINGS`, add/confirm:

```python
Binding("ctrl+p", "command_palette", "command palette"),
Binding("f1", "help", "help"),
```

Add action:

```python
def action_command_palette(self):
    def _fill(command):
        if command:
            self.chat_input.value = command + " "
            self.chat_input.focus()
    self.push_screen(CommandPalette(), callback=_fill)
```

- [ ] **Step 3: Test palette opens and returns a command**

```python
def test_command_palette_fills_input(pilot):
    pilot.app.action_command_palette()
    assert isinstance(pilot.app.screen, CommandPalette)
    pilot.app.screen.dismiss("/chaos ")
    assert pilot.app.chat_input.value == "/chaos "
```

- [ ] **Step 4: Run test**

Run: `uv run pytest tests/test_tui_keys.py::test_command_palette_fills_input -v`
Expected: PASS after implementation.

- [ ] **Step 5: Commit**

```bash
git add src/replicanta/tui.py tests/test_tui_keys.py
git commit --no-verify -m "feat(tui): searchable command palette"
```

---

## Task 3: Inline slash-command hints above the chat input

**Files:**
- Modify: `src/replicanta/tui.py`
- Test: `tests/test_tui_keys.py`

- [ ] **Step 1: Add a hint widget**

```python
class CommandHints(Static):
    """Renders filtered command hints above the input."""

    def update_for(self, value):
        if not value.startswith("/"):
            self.update("")
            return
        parts = value.split(None, 1)
        query = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        items = tui_commands.filter_commands(query)
        if prefix:
            # Show subcommand hint if we know the command
            items = [c for c in items if c[0] == query]
            if items:
                self.update(f"Usage: {items[0][1]}")
                return
        lines = [f"{c[1]:<16} {c[2]}" for c in items[:8]]
        self.update("\n".join(lines) if lines else "")
```

- [ ] **Step 2: Mount it above the chat input**

In `OrganismApp.compose`, place `CommandHints` above the `Input`.

- [ ] **Step 3: Update on input changes**

```python
def on_input_changed(self, event):
    self.query_one(CommandHints).update_for(event.value)
```

- [ ] **Step 4: Test hints appear**

```python
def test_command_hints_filter_on_slash(pilot):
    pilot.app.chat_input.value = "/voi"
    pilot.app.on_input_changed(Input.Changed(chat_input, "/voi"))
    hints = pilot.app.query_one(CommandHints)
    assert "voice" in hints.renderable.lower()
```

- [ ] **Step 5: Run test and commit**

Run: `uv run pytest tests/test_tui_keys.py -v`
Expected: PASS

```bash
git add src/replicanta/tui.py tests/test_tui_keys.py
git commit --no-verify -m "feat(tui): inline slash-command hints"
```

---

## Task 4: Visible tab bar

**Files:**
- Modify: `src/replicanta/tui.py`, `src/replicanta/tui_views.py`
- Test: `tests/test_tui_keys.py`

- [ ] **Step 1: Add a `TabBar` widget**

```python
class TabBar(Widget):
    """Clickable/keyboard tab bar."""

    def __init__(self, tabs, active, **kwargs):
        super().__init__(**kwargs)
        self.tabs = tabs
        self.active = active

    def render(self):
        parts = []
        for label, key in self.tabs:
            style = "reverse" if label == self.active else ""
            parts.append(f"[{style}]{label}[/]")
        return "  ".join(parts)

    def set_active(self, label):
        self.active = label
        self.refresh()
```

- [ ] **Step 2: Mount tab bar and bind clicks**

In `OrganismApp.compose`, add `TabBar` above `TabbedContent`. Bind click to
switch tabs:

```python
def on_click(self, event):
    widget, _ = self.get_widget_at(event.screen_x, event.screen_y)
    if isinstance(widget, TabBar):
        # Determine tab from x position or use a Button approach
        ...
```

Prefer using `Button` widgets inside `TabBar` for click handling.

- [ ] **Step 3: Keep tab bar in sync with tab changes**

Override `action_show_tab` to also update `TabBar`.

- [ ] **Step 4: Test tab bar is visible and clickable**

```python
def test_tab_bar_labels_visible(pilot):
    bar = pilot.app.query_one(TabBar)
    text = str(bar.render())
    assert "Chat" in text
    assert "Mind" in text
    assert "Memory" in text
```

- [ ] **Step 5: Run test and commit**

Run: `uv run pytest tests/test_tui_keys.py -v`
Expected: PASS

```bash
git add src/replicanta/tui.py src/replicanta/tui_views.py tests/test_tui_keys.py
git commit --no-verify -m "feat(tui): visible tab bar"
```

---

## Task 5: Activity indicator in status bar

**Files:**
- Modify: `src/replicanta/tui.py`, `src/replicanta/tui_views.py`
- Test: `tests/test_tui_keys.py`

- [ ] **Step 1: Add `ActivityLabel` widget**

```python
class ActivityLabel(Static):
    def show(self, text):
        self.update(f"{text}")
        self.styles.display = "block"

    def clear(self):
        self.update("")
        self.styles.display = "none"
```

- [ ] **Step 2: Add helper to push activity**

In `OrganismApp`:

```python
def set_activity(self, text):
    self.query_one(ActivityLabel).show(text)

@property
def activity_text(self):
    return str(self.query_one(ActivityLabel).renderable)
```

- [ ] **Step 3: Hook existing background workers**

Update `_respond`, `_narrate`, `_self_talk`, `_ask_user`, MUD worker to call
`set_activity` before starting and `clear` in `finally`.

- [ ] **Step 4: Test activity indicator**

```python
def test_activity_shows_during_response(pilot, monkeypatch):
    monkeypatch.setattr(
        "replicanta.voice.respond", lambda *a, **k: "hello"
    )
    pilot.app.action_respond("hi")
    assert "thinking" in pilot.app.activity_text.lower()
```

- [ ] **Step 5: Run test and commit**

Run: `uv run pytest tests/test_tui_keys.py -v`
Expected: PASS

```bash
git add src/replicanta/tui.py src/replicanta/tui_views.py tests/test_tui_keys.py
git commit --no-verify -m "feat(tui): activity indicator in status bar"
```

---

## Task 6: Pending-mutation banner

**Files:**
- Modify: `src/replicanta/tui.py`
- Test: `tests/test_tui_keys.py`

- [ ] **Step 1: Add `MutationBanner` widget**

```python
class MutationBanner(Horizontal):
    def compose(self):
        yield Static(id="mutation-summary")
        yield Button("Approve", id="mutation-approve", variant="success")
        yield Button("Reject", id="mutation-reject", variant="error")
        yield Button("Why?", id="mutation-why")
```

- [ ] **Step 2: Show/hide based on pending patch**

In `_refresh_views` or a timer, check `extensions.registry().get("pending")`:

```python
def _update_mutation_banner(self):
    pending = extensions.registry().get("pending")
    banner = self.query_one(MutationBanner)
    if pending:
        banner.query_one("#mutation-summary", Static).update(
            f"Pending patch: {pending.get('kind', 'unknown')}"
        )
        banner.styles.display = "block"
    else:
        banner.styles.display = "none"
```

- [ ] **Step 3: Wire buttons**

```python
def on_button_pressed(self, event):
    if event.button.id == "mutation-approve":
        extensions.approve(self.org.extension_path)
    elif event.button.id == "mutation-reject":
        extensions.reject(self.org.extension_path)
    elif event.button.id == "mutation-why":
        self._show_mutation_why()
    self._update_mutation_banner()
```

- [ ] **Step 4: Test banner visibility**

```python
def test_mutation_banner_shows_when_pending(pilot, monkeypatch):
    monkeypatch.setattr(
        "replicanta.extensions.registry",
        lambda: {"pending": {"kind": "rule"}},
    )
    pilot.app._update_mutation_banner()
    assert pilot.app.query_one(MutationBanner).styles.display != "none"
```

- [ ] **Step 5: Run test and commit**

Run: `uv run pytest tests/test_tui_keys.py -v`
Expected: PASS

```bash
git add src/replicanta/tui.py tests/test_tui_keys.py
git commit --no-verify -m "feat(tui): pending-mutation approval banner"
```

---

## Task 7: Sidebar action buttons

**Files:**
- Modify: `src/replicanta/tui.py`
- Test: `tests/test_tui_layout.py`

- [ ] **Step 1: Add `QuickActions` widget**

```python
class QuickActions(Vertical):
    def compose(self):
        yield Button("Sleep / Wake", id="qa-sleep")
        yield Button("Voice", id="qa-voice")
        yield Button("Listen", id="qa-listen")
        yield Button("Look", id="qa-look")
        yield Button("MUD", id="qa-mud")
```

- [ ] **Step 2: Mount in sidebar and wire to existing actions**

```python
def on_button_pressed(self, event):
    mapping = {
        "qa-sleep": self.action_sleep_wake,
        "qa-voice": self.action_voice,
        "qa-listen": self.action_talk,
        "qa-look": self.action_look,
        "qa-mud": self.action_mud,
    }
    action = mapping.get(event.button.id)
    if action:
        action()
```

- [ ] **Step 3: Update button labels from state**

```python
def _update_quick_actions(self):
    voice_btn = self.query_one("#qa-voice", Button)
    voice_btn.label = "Voice: on" if speech.enabled else "Voice: off"
```

- [ ] **Step 4: Test buttons exist**

```python
def test_quick_actions_buttons_exist(pilot):
    for bid in ("qa-sleep", "qa-voice", "qa-listen", "qa-look", "qa-mud"):
        assert pilot.app.query_one(f"#{bid}", Button)
```

- [ ] **Step 5: Run test and commit**

Run: `uv run pytest tests/test_tui_layout.py -v`
Expected: PASS

```bash
git add src/replicanta/tui.py tests/test_tui_layout.py
git commit --no-verify -m "feat(tui): sidebar quick-action buttons"
```

---

## Task 8: Toast-style error feedback

**Files:**
- Modify: `src/replicanta/tui.py`
- Test: `tests/test_tui_keys.py`

- [ ] **Step 1: Add `Toast` widget and `show_toast` helper**

```python
class Toast(Static):
    def show(self, message, duration=3.0):
        self.update(message)
        self.styles.display = "block"
        self.set_timer(duration, lambda: self.styles.update(display="none"))


def show_toast(self, message):
    self.query_one(Toast).show(message)
```

- [ ] **Step 2: Replace silent failures with toasts**

Find places that currently swallow errors (camera, microphone, voice
lookup) and call `self.show_toast(...)`.

- [ ] **Step 3: Test toast shows**

```python
def test_toast_shows_message(pilot):
    pilot.app.show_toast("Camera not found")
    toast = pilot.app.query_one(Toast)
    assert "Camera not found" in str(toast.render())
```

- [ ] **Step 4: Run test and commit**

Run: `uv run pytest tests/test_tui_keys.py -v`
Expected: PASS

```bash
git add src/replicanta/tui.py tests/test_tui_keys.py
git commit --no-verify -m "feat(tui): toast-style error feedback"
```

---

## Task 9: Empty states for Mind/Memory/Inner tabs

**Files:**
- Modify: `src/replicanta/tui_views.py`, `src/replicanta/tui.py`
- Test: `tests/test_tui_views.py`

- [ ] **Step 1: Add empty-state helpers**

```python
def empty_mind():
    return Static("No beliefs yet. Tell the organism something about yourself.")


def empty_memory():
    return Static("No memories yet. Memories form as you talk.")


def empty_inner():
    return Static(
        "Mental-state gauges appear here: mood, stress, grounding, chaos, "
        "and recent thought metabolism."
    )
```

- [ ] **Step 2: Render them when content is empty**

In `tui.py` tab builders, check if data is empty and mount the helper.

- [ ] **Step 3: Test empty states**

```python
def test_empty_mind_renders(org):
    widget = tui_views.empty_mind()
    assert "beliefs" in str(widget.render()).lower()
```

- [ ] **Step 4: Run test and commit**

Run: `uv run pytest tests/test_tui_views.py -v`
Expected: PASS

```bash
git add src/replicanta/tui_views.py src/replicanta/tui.py tests/test_tui_views.py
git commit --no-verify -m "feat(tui): empty states for mind/memory/inner tabs"
```

---

## Task 10: Glasshouse command bar and tab bar

**Files:**
- Modify: `src/replicanta/web_static.py`, `src/replicanta/web.py`
- Test: `tests/test_web.py`

- [ ] **Step 1: Add command input and tab buttons to HTML**

Update `APP_HTML` to include:

```html
<nav id="tabs">
  <button data-view="habitat" class="on">Habitat</button>
  <button data-view="atlas">Mind</button>
  <button data-view="memory">Memory</button>
  <button data-view="inner">Inner</button>
  <button data-view="cells">Cells</button>
  <button data-view="mud">MUD</button>
</nav>
...
<form id="command"><input placeholder="Type / for commands…"></form>
```

- [ ] **Step 2: Add `/api/command` endpoint in `web.py`**

```python
def do_POST(self):
    ...
    elif path == "/api/command":
        return self._command(data)


def _command(self, data):
    text = str(data.get("text", "")).strip()
    if not text:
        raise WebError("empty command")
    if text.startswith("/"):
        return self._handle_slash(text)
    return self.app.chat(text)
```

- [ ] **Step 3: Add `/api/commands` metadata endpoint**

```python
def _commands(self):
    return [
        {"name": c[0], "usage": c[1], "description": c[2], "category": c[3]}
        for c in tui_commands.COMMANDS
    ]
```

- [ ] **Step 4: Update JS for command bar and hints**

```javascript
$('#command').onsubmit=async e=>{e.preventDefault();const t=$('#command input');if(!t.value.trim())return;t.disabled=true;try{render((await api('command',{text:t.value})).state)}catch(x){showToast(x.message)}finally{t.disabled=false;t.focus()}};
```

Add a `showToast` function and inline hint rendering on `/` input.

- [ ] **Step 5: Test web command endpoint**

```python
def test_web_command_endpoint(live):
    r = requests.post(f"{live}/api/command", json={"text": "/stats"})
    assert r.ok
    assert "state" in r.json()
```

- [ ] **Step 6: Run test and commit**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS

```bash
git add src/replicanta/web_static.py src/replicanta/web.py tests/test_web.py
git commit --no-verify -m "feat(web): command bar and tab bar"
```

---

## Task 11: Glasshouse stewardship panel and activity indicator

**Files:**
- Modify: `src/replicanta/web_static.py`, `src/replicanta/web.py`
- Test: `tests/test_web.py`

- [ ] **Step 1: Expand stewardship HTML**

Add toggles/buttons for voice, listen, look, self-talk, auto-apply, git,
and an activity indicator.

- [ ] **Step 2: Add `/api/settings` extensions for voice/self-talk/git**

```python
def settings(self, data):
    ...
    if "voice" in data:
        speech.set_enabled(bool(data["voice"]))
    if "self_talk" in data:
        self.org.store.self_talk = bool(data["self_talk"])
        self.org.store.dirty = True
    if "git" in data:
        self.org.config["git"] = {"enabled": bool(data["git"])}
    ...
```

- [ ] **Step 3: Update JS to call settings endpoints**

Bind each toggle/button to `api('settings', {...})` and refresh state.

- [ ] **Step 4: Test settings endpoints**

```python
def test_web_settings_voice(live):
    r = requests.post(f"{live}/api/settings", json={"voice": True})
    assert r.ok
    assert r.json()["organism"]["voice"] is True
```

- [ ] **Step 5: Run test and commit**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS

```bash
git add src/replicanta/web_static.py src/replicanta/web.py tests/test_web.py
git commit --no-verify -m "feat(web): stewardship panel and activity indicator"
```

---

## Task 12: Final integration and regression testing

**Files:**
- All changed files.

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Run linting**

Run: `uv run ruff check --ignore I001,UP017 .`
Expected: clean.

- [ ] **Step 3: Manual smoke checks**

Start the TUI with a test organism and verify:
- F1 opens command palette.
- `/` in chat shows hints.
- Tab bar is visible and clickable.
- Activity indicator appears during a response.
- Mutation banner appears when a patch is pending.
- Sidebar buttons work.

Start Glasshouse with `--web` and verify:
- Tab bar visible.
- Command bar accepts `/` commands.
- Stewardship toggles update state.

- [ ] **Step 4: Merge to main**

```bash
git checkout main
git merge --no-ff <branch> -m "feat(ui): TUX redesign — palette, tab bar, feedback, web parity"
git push origin main
```

---

## Spec coverage check

| Spec section | Task |
|--------------|------|
| 1.1 Header bar | implicitly covered by status/activity work; add if needed |
| 1.2 Sidebar navigator | Task 7 (quick actions); full sidebar restructure out of scope |
| 1.3 Visible tab bar | Task 4 |
| 1.4 Bottom status bar | Task 5 + existing bar |
| 1.5 MUD tab | Task 4 (add MUD tab) + existing MUD overlay |
| 2.1 Command palette | Task 2 |
| 2.2 Inline hints | Task 3 |
| 2.3 Sidebar action buttons | Task 7 |
| 2.4 Confirm destructive actions | future task if required |
| 2.5 Searchable history | future task if required |
| 3.1 Activity indicator | Task 5 |
| 3.2 Structured system messages | future task if required |
| 3.3 Pending-mutation banner | Task 6 |
| 3.4 Toast errors | Task 8 |
| 3.5 Empty states | Task 9 |
| 4.1 Web command bar | Task 10 |
| 4.2 Web tab navigation | Task 10 |
| 4.3 Stewardship panel | Task 11 |
| 4.4 Mutation approval UI | Task 10/11 |
| 4.5 Keyboard shortcuts | Task 10 |
| 4.6 Responsive layout | Task 10/11 CSS |

Gaps: destructive-action confirmations, searchable history, structured
system messages, full sidebar restructure. These are explicitly marked as
out-of-scope or future tasks to keep the plan shippable.
