# TUI Redesign: Neoism-Style Workspace Chrome

## Goal

Transform the Replicanta TUI from a functional but plain tabbed interface into a polished, Neoism-inspired workspace chrome: clean top/bottom bars, a left organism sidebar, smooth scrolling, consistent card styling, and prominent organism identity.

## Motivation

Current pain points:

- Organism names are not displayed prominently.
- Chat text blocks feel like raw log output rather than styled cards.
- Header and Footer are stock Textual widgets with no project-specific chrome.
- Mind/Memory/Inner panes can clip content because `Static` does not scroll by default.
- The overall layout lacks the unified workspace feel of reference apps like Neoism.

## Design

### Layout

```
┌────────────────────────────────────────────────────────────────────────┐
│  Replicanta  │  🧠 selfhearing · calm · a/r/i 0.27/0.54/0.05 │ 🎙 🕙 10:17 │
├──────────┬─────────────────────────────────────────────────────────────┤
│          │  [chat]  [mind]  [memory]  [inner]                          │
│ nursery  │                                                             │
│  ● self  │  ┌─────────────────────────────────────────────────────┐   │
│    fern  │  │                                                     │   │
│    moss  │  │              main content area                      │   │
│          │  │                                                     │   │
│          │  └─────────────────────────────────────────────────────┘   │
│          │                                                             │
├──────────┴─────────────────────────────────────────────────────────────┤
│  12 beliefs · 39 rules · cycle 104 · voice online · ctrl+p palette ·    │
│  F1 help · F2-F7 tabs · ctrl+q quit                                     │
└────────────────────────────────────────────────────────────────────────┘
```

### Components

#### Top bar

A custom `Static` replaces the stock `Header`.

- Left: app wordmark **Replicanta**.
- Center: current organism display:
  - Lifecycle icon (🧠 awake / 💤 sleeping / 🪦 faded).
  - Organism name: learned name from beliefs if available, otherwise directory name.
  - Mood and mental-state summary (`a/r/i 0.27/0.54/0.05`).
- Right: status icons for microphone/voice and a UTC clock.

Updated by `refresh_top_bar()`, called from `_show_org()` and `_on_tick()`.

#### Left sidebar

A `Vertical` sidebar containing the organism nursery navigator.

- Header: **nursery**.
- List of organisms discovered under `organisms/` (and the legacy root organism if present).
- Current organism highlighted with `●`.
- Clicking an organism entry swaps to it (reuses `/swap` logic).
- Keyboard: `/swap` and `/organisms` still work; consider future `j/k` navigation.

Updated by `_refresh_sidebar()`, called from `_show_org()` and after `/new`, `/swap`, or `/organisms`.

#### Main content area

The existing `TabbedContent` is kept but styled more cleanly:

- Tabs: chat / mind / memory / inner.
- Active tab uses an underline/highlight style.
- Each pane is wrapped in a `VerticalScroll` so long content scrolls with a visible scrollbar.
- Chat log cards are rendered as consistent `Rich` panels with uniform padding, borders, and color coding.
- Pending reply line remains above the input.

#### Bottom status line

A custom `Static` replaces the stock `Footer`.

- Left: live counts (`12 beliefs · 39 rules · cycle 104`).
- Right: key hints (`ctrl+p palette · F1 help · F2-F7 tabs · ctrl+q quit`).
- Single or two-row layout depending on terminal width; Textual wrapping handles narrow screens.

Updated by `refresh_status()`.

### Visual System

CSS palette uses Textual variables:

- `$primary` for active tabs and highlights.
- `$surface` for sidebar and status bars.
- `$success` / `$warning` / `$error` for mood and lifecycle states.
- `$text` and `$text-muted` for primary and dim text.

Specific rules:

- `#topbar` and `#bottombar`: fixed height, full width, padding, background `$surface`.
- `#sidebar`: width 20-24, background `$surface`, right border.
- `#sidebar .current`: bold / highlighted.
- `TabbedContent` tabs: minimal, with active underline.
- Chat cards: `border: round` panels; user cyan, organism green, system dim.
- Scrollable panes: `VerticalScroll` with `overflow-y: auto` and visible scrollbar.

### Data Flow

- `on_mount()` calls `_show_org()` which sets top bar, sidebar, and initial panes.
- `_on_tick()` advances the organism, then calls:
  - `refresh_top_bar()`
  - `refresh_status()`
  - `_refresh_views()`
- `_refresh_views()` updates mind/memory/inner Static content inside `VerticalScroll`.
- `_refresh_sidebar()` reads `self.root / "organisms"` and rebuilds the sidebar list.
- `/swap`, `/new`, `/organisms` trigger `_show_org()` to refresh chrome.

### Files to Change

- `tui.py` — layout, compose, CSS, new chrome methods, binding/keyboard behavior.
- `tui_views.py` — chat card formatter; keep `mind_view`, `memory_view`, `inner_view`, `inner_renderable` but ensure they render well inside scrollable containers.
- `tests/test_tui_keys.py` — add layout/regression tests.
- `tests/test_tui_layout.py` — new tests for top bar, sidebar, bottom bar content.

### Testing

- Existing tests must continue to pass.
- New tests:
  - Top bar shows organism name and lifecycle icon.
  - Sidebar lists organisms and highlights the current one.
  - Bottom bar shows belief/rule counts and key hints.
  - Mind/Memory/Inner panes are inside scrollable containers.
  - Chat cards render as panels.

### Out of Scope

- Mouse drag-to-resize sidebar.
- Theme switching at runtime.
- Animations beyond Textual's built-in transitions.
- Rewriting the RichLog as a full message list component.

## Success Criteria

- The TUI looks and feels like a unified workspace, not a stock tabbed app.
- Organism identity is visible at a glance.
- Long Mind/Memory/Inner content scrolls smoothly.
- All existing functionality (commands, F-keys, /swap, etc.) remains intact.
- All tests pass and the live TUI renders correctly.
