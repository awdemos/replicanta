# TUI Neoism-Style Workspace Chrome Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Replicanta TUI layout with a Neoism-style workspace chrome: custom top/bottom bars, left organism sidebar, scrollable panes, and polished chat cards.

**Architecture:** Replace the stock `Header`/`Footer` and single-line `Static` status with project-specific chrome widgets. Wrap non-chat tabs in `VerticalScroll` for smooth scrolling. Add a nursery sidebar using `ListView`. Keep the existing view builders and event pipeline; only the chrome and compose layout change.

**Tech Stack:** Python 3.14, Textual 8.x, rich (renderables), pytest for headless TUI tests.

---

## File map

- `tui.py` — main app. Compose, CSS, chrome update methods, sidebar interactions.
- `tui_views.py` — chat card formatter and organism-name helper.
- `tests/test_tui_layout.py` — new tests for chrome content and layout.
- `tests/test_tui_keys.py` — extend with sidebar selection and scrollable-pane tests.

---

## Task 1: Add chrome update helpers

**Files:**
- Modify: `tui.py:119-129` (CSS block)
- Modify: `tui.py:156-162` (`__init__` fields)
- Modify: `tui.py:201-218` (`_show_org`)
- Modify: `tui.py:602-627` (`refresh_status`)

- [ ] **Step 1: Add helper fields and methods**

  In `OrganismApp.__init__`, add:
  ```python
  self._topbar_text = ""
  self._bottombar_text = ""
  ```

  Add three new methods after `action_quit`:
  ```python
  def refresh_top_bar(self):
      """Render the custom top bar: app wordmark, organism identity,
      mood/mental state, voice/mic/clock icons."""
      store = self.org.store
      state_word = self.org.lifecycle.state
      icon = {"awake": "🧠", "asleep": "💤", "faded": "🪦"}.get(state_word, "🧠")
      name = self._org_display_name()
      mood = next(
          (v for (o, a, v) in store.beliefs() if (o, a) == ("self", "mood")),
          "calm")
      mental = (f"a/r/i {store.arousal:.2f}/{store.rationality:.2f}/"
                f"{store.irrationality:.2f}")
      mic = " 🎙" if getattr(self.listener, "recording", False) else ""
      spoken = " 🔊" if speech.enabled else ""
      voice = narration.voice_status()
      clock = self.org.probe.clock_utc()
      text = (f"Replicanta  │  {icon} {name} · {mood} · {mental}"
              f"  │  {voice}{mic}{spoken}  {clock}")
      self._topbar_text = text
      self.query_one("#topbar", Static).update(text)

  def _org_display_name(self):
      """Prefer the organism's learned name, fall back to directory name."""
      beliefs = self.org.store.beliefs()
      learned = next(
          (v for (o, a, v) in beliefs if (o, a) == ("self", "name")), None)
      if learned:
          return learned
      return Path(self.org.dir_path).name

  def _refresh_sidebar(self):
      """Rebuild the nursery sidebar, highlighting the current organism."""
      items = []
      organisms_dir = self.root / "organisms"
      current = Path(self.org.dir_path).name
      if organisms_dir.is_dir():
          for path in sorted(organisms_dir.iterdir()):
              if path.is_dir():
                  marker = "● " if path.name == current else "  "
                  items.append(f"{marker}{path.name}")
      # legacy root organism
      legacy = self.root / "state.json"
      if legacy.exists():
          marker = "● " if Path(self.org.dir_path).name == "default" else "  "
          items.append(f"{marker}default")
      text = "nursery\n" + "\n".join(items) if items else "nursery\n  (no organisms)"
      self.query_one("#sidebar-list", Static).update(text)
  ```

- [ ] **Step 2: Wire helpers into existing update paths**

  In `_show_org`, replace the existing `self.refresh_status(); self._refresh_views()` block with:
  ```python
  self.refresh_top_bar()
  self._refresh_sidebar()
  self.refresh_status()
  self._refresh_views()
  ```

  In `refresh_status`, update `#bottombar` instead of `#status`:
  ```python
  self.query_one("#bottombar", Static).update(self._bottombar_text)
  ```
  and change the method to build:
  ```python
  self._bottombar_text = (
      f"{m.belief_count} beliefs · {m.rule_count} rules · "
      f"cycle {self.org.store.cycle} · {narration.voice_status()}"
      f"{spoken}{mic}{playing}  │  "
      "ctrl+p palette · F1 help · F2-F7 tabs · ctrl+q quit")
  ```

- [ ] **Step 3: Run existing tests**

  Run: `.venv/bin/python -m pytest tests/test_tui_keys.py tests/test_tui_commands.py -q`
  Expected: PASS (helpers are not yet called by compose, so behavior unchanged).

- [ ] **Step 4: Commit**

  ```bash
  git add tui.py
  git commit -m "refactor(tui): add chrome helper methods for top bar and sidebar"
  ```

---

## Task 2: Rewrite compose() and CSS for workspace chrome

**Files:**
- Modify: `tui.py:106-118` (BINDINGS)
- Modify: `tui.py:119-129` (CSS)
- Modify: `tui.py:164-186` (compose)

- [ ] **Step 1: Update imports**

  Ensure `tui.py` imports:
  ```python
  from textual.containers import Horizontal, Vertical, VerticalScroll
  ```
  (Already imports `Static`, `Input`, `RichLog`, etc.)

- [ ] **Step 2: Replace CSS block**

  Replace the existing `CSS` string with:
  ```python
  CSS = """
  #topbar { height: 1; padding: 0 1; background: $surface; color: $text; }
  #main { height: 1fr; }
  #sidebar { width: 24; background: $surface; color: $text; border-right: solid $primary; }
  #sidebar-header { height: 1; padding: 0 1; background: $surface; color: $text-muted; text-style: bold; }
  #sidebar-list { padding: 0 1; }
  #content { width: 1fr; height: 1fr; }
  TabbedContent { height: 1fr; }
  #dreams { height: 1fr; padding: 0 1; }
  #pending { height: auto; max-height: 4; padding: 0 1; color: $success; }
  #mind, #memory, #inner { padding: 1 2; }
  #inner { overflow-y: auto; }
  #chat { height: 3; border: solid yellow; }
  #bottombar { height: 1; padding: 0 1; background: $surface; color: $text-muted; }
  """
  ```

- [ ] **Step 3: Rewrite compose()**

  Replace `compose` with:
  ```python
  def compose(self) -> ComposeResult:
      yield Static("", id="topbar")
      with Horizontal(id="main"):
          with Vertical(id="sidebar"):
              yield Static("nursery", id="sidebar-header")
              yield Static("", id="sidebar-list")
          with Vertical(id="content"):
              with TabbedContent(initial="chat-pane"):
                  with TabPane("chat", id="chat-pane"):
                      dreams = RichLog(
                          id="dreams", max_lines=1000, wrap=True,
                          markup=True, highlight=False)
                      dreams.can_focus = False
                      yield dreams
                      yield Static("", id="pending", markup=False)
                  with TabPane("mind", id="mind-pane"):
                      with VerticalScroll():
                          yield Static("", id="mind", markup=False)
                  with TabPane("memory", id="memory-pane"):
                      with VerticalScroll():
                          yield Static("", id="memory", markup=False)
                  with TabPane("inner", id="inner-pane"):
                      with VerticalScroll():
                          yield Static("", id="inner", markup=False)
      self.chat_input = Input(
          placeholder="talk to me, or /help …  (tab completes · "
                      "F2 chat · F3 mind · F4 memory · F7 inner)",
          id="chat")
      yield self.chat_input
      yield Static("", id="bottombar")
  ```

- [ ] **Step 4: Run layout tests**

  Run: `.venv/bin/python -m pytest tests/test_tui_keys.py -q`
  Expected: PASS (widgets exist; existing tests check tab switching/focus).

- [ ] **Step 5: Commit**

  ```bash
  git add tui.py
  git commit -m "feat(tui): workspace chrome layout with top bar, sidebar, bottom bar"
  ```

---

## Task 3: Add sidebar click-to-swap

**Files:**
- Modify: `tui.py:164-186` (compose, wrap sidebar entries)
- Modify: `tui.py:220-237` (`_swap_to`)

- [ ] **Step 1: Replace sidebar Static with ListView**

  Update imports:
  ```python
  from textual.widgets import ListView, ListItem, Label
  ```

  Update compose sidebar block:
  ```python
  with Vertical(id="sidebar"):
      yield Static("nursery", id="sidebar-header")
      yield ListView(id="sidebar-list")
  ```

- [ ] **Step 2: Rewrite _refresh_sidebar to populate ListView**

  ```python
  def _refresh_sidebar(self):
      """Rebuild the nursery sidebar, highlighting the current organism."""
      lv = self.query_one("#sidebar-list", ListView)
      lv.clear()
      organisms_dir = self.root / "organisms"
      current = Path(self.org.dir_path).name
      names = []
      if organisms_dir.is_dir():
          names = sorted(p.name for p in organisms_dir.iterdir() if p.is_dir())
      if not names:
          lv.append(ListItem(Label("(no organisms)")))
          return
      for name in names:
          marker = "● " if name == current else "  "
          lv.append(ListItem(Label(f"{marker}{name}"), name=name))
  ```

- [ ] **Step 3: Add selection handler**

  Add method:
  ```python
  def on_list_view_selected(self, event):
      """Sidebar organism selection swaps to that organism."""
      if event.item.name and event.item.name != Path(self.org.dir_path).name:
          self._swap_to(event.item.name)
  ```

- [ ] **Step 4: Add CSS for selected list item**

  Add to CSS:
  ```
  #sidebar-list { padding: 0; height: 1fr; border: none; background: $surface; }
  #sidebar-list > ListItem { padding: 0 1; }
  #sidebar-list > ListItem.--highlight { background: $primary; color: $text; }
  ```

- [ ] **Step 5: Run tests**

  Run: `.venv/bin/python -m pytest tests/test_tui_keys.py -q`
  Expected: PASS.

- [ ] **Step 6: Commit**

  ```bash
  git add tui.py
  git commit -m "feat(tui): clickable organism sidebar with ListView"
  ```

---

## Task 4: Polish chat cards

**Files:**
- Modify: `tui_views.py` (new card formatter)
- Modify: `tui.py` (`_log_chat`, `_write_card`, `_append_log`)

- [ ] **Step 1: Add card formatter in tui_views.py**

  ```python
  from rich.panel import Panel
  from rich.text import Text

  STYLE_USER = "cyan"
  STYLE_ORG = "green"
  STYLE_DIM = "dim"

  def chat_card(who, text, timestamp=None, border_style=None):
      """A consistent panel card for chat utterances."""
      border_style = border_style or (STYLE_USER if who == "you" else STYLE_ORG)
      title = f"{who} · {timestamp}" if timestamp else who
      return Panel(
          Text(text),
          title=title,
          title_align="left",
          border_style=border_style,
          padding=(0, 1),
      )
  ```

- [ ] **Step 2: Update _write_card to use Panel renderable**

  In `tui.py`, change `_write_card` to:
  ```python
  def _write_card(self, who, text, border_style, stamp=True):
      ts = self._stamp() if stamp else None
      card = tui_views.chat_card(who, text, timestamp=ts,
                                 border_style=border_style)
      self.query_one("#dreams", RichLog).write(card)
      self.query_one("#dreams", RichLog).write("")
  ```

- [ ] **Step 3: Remove duplicated blank-line logic**

  Ensure `_log_chat` calls `_write_card` without adding extra blank lines elsewhere.

- [ ] **Step 4: Run view tests**

  Run: `.venv/bin/python -m pytest tests/test_tui_views.py tests/test_tui_keys.py -q`
  Expected: PASS (existing card tests may need updating; see Task 6).

- [ ] **Step 5: Commit**

  ```bash
  git add tui.py tui_views.py
  git commit -m "feat(tui): render chat cards as rich panels"
  ```

---

## Task 5: Remove obsolete Header/Footer references

**Files:**
- Modify: `tui.py` imports
- Modify: `tui.py` docstring

- [ ] **Step 1: Clean imports**

  Remove `Header` and `Footer` from the `textual.widgets` import list if no longer used.

- [ ] **Step 2: Update class docstring**

  Update the class docstring to reflect the new chrome instead of mentioning Header/Footer explicitly.

- [ ] **Step 3: Commit**

  ```bash
  git add tui.py
  git commit -m "chore(tui): drop Header/Footer imports; update docstring"
  ```

---

## Task 6: Add layout regression tests

**Files:**
- Create: `tests/test_tui_layout.py`
- Modify: `tests/test_tui_keys.py`

- [ ] **Step 1: Create tests/test_tui_layout.py**

  ```python
  """Layout regression tests for the workspace chrome."""

  import asyncio
  import sys
  from pathlib import Path

  from textual.containers import VerticalScroll
  from textual.widgets import ListView, Static, TabbedContent

  sys.path.insert(0, str(Path(__file__).parent.parent))

  from organism import Organism
  from tui import OrganismApp


  def _headless_app(monkeypatch, tmp_path):
      org = Organism(tmp_path)
      org.load()
      app = OrganismApp(org)
      monkeypatch.setattr(app, "_probe_voice", lambda: None)
      monkeypatch.setattr(app, "_maybe_narrate", lambda: None)
      monkeypatch.setattr(app, "_on_tick", lambda: None)
      return app


  def test_top_bar_shows_organism_name(monkeypatch, tmp_path):
      app = _headless_app(monkeypatch, tmp_path)

      async def check():
          async with app.run_test():
              app.refresh_top_bar()
              top = app.query_one("#topbar", Static)
              assert "Replicanta" in str(top.renderable)
              name = Path(app.org.dir_path).name
              assert name in str(top.renderable)

      asyncio.run(check())


  def test_sidebar_lists_organisms_and_highlights_current(monkeypatch, tmp_path):
      app = _headless_app(monkeypatch, tmp_path)
      (app.root / "organisms" / "fern").mkdir(parents=True)

      async def check():
          async with app.run_test():
              app._refresh_sidebar()
              lv = app.query_one("#sidebar-list", ListView)
              current = Path(app.org.dir_path).name
              assert any(current in str(item.render()) for item in lv.children)
              assert any("fern" in str(item.render()) for item in lv.children)

      asyncio.run(check())


  def test_bottom_bar_shows_counts_and_keys(monkeypatch, tmp_path):
      app = _headless_app(monkeypatch, tmp_path)

      async def check():
          async with app.run_test():
              app.refresh_status()
              bottom = app.query_one("#bottombar", Static)
              text = str(bottom.renderable)
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
  ```

- [ ] **Step 2: Run new tests**

  Run: `.venv/bin/python -m pytest tests/test_tui_layout.py -q`
  Expected: PASS.

- [ ] **Step 3: Commit**

  ```bash
  git add tests/test_tui_layout.py
  git commit -m "test(tui): layout regression tests for chrome"
  ```

---

## Task 7: Full verification

**Files:** all changed.

- [ ] **Step 1: Run full test suite**

  Run: `.venv/bin/python -m pytest -q`
  Expected: all tests PASS.

- [ ] **Step 2: Run CI ruff**

  Run: `.venv/bin/ruff check --ignore I001,UP017 .`
  Expected: All checks passed!

- [ ] **Step 3: Restart live TUI and inspect**

  Kill the existing tmux TUI process and restart:
  ```bash
  kill <pid>
  tmux send-keys -t orgtui-fix:1.1 "cd /var/home/a/code/replicanta && .venv/bin/python -m tui --dir /home/a/.tmp/opencode/replicanta-selfhearing --org selfhearing --chaos 0" Enter
  ```
  Capture the pane and verify: top bar, sidebar, bottom bar, tabs, and scrolling.

- [ ] **Step 4: Final commit and push**

  ```bash
  git push origin main
  ```

---

## Spec coverage check

| Spec requirement | Task |
|---|---|
| Custom top bar with organism identity | Task 1, 2 |
| Left sidebar listing organisms | Task 2, 3 |
| Bottom status line | Task 1, 2 |
| Scrollable mind/memory/inner | Task 2 |
| Polished chat cards | Task 4 |
| Cohesive CSS palette | Task 2 |
| Tests | Task 6, 7 |

---

**Completed as of 2026-08-10.** The chrome, sidebar, scrollable panes, chat cards,
F8 cells tab, and related tests are already implemented in `tui.py`,
`tui_views.py`, `tests/test_tui_views.py`, and `tests/test_tui_keys.py`.
Archived to completed plans.

