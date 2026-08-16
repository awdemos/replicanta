# Replicanta TUI + Glasshouse UX Redesign

## Goal

Make the Replicanta terminal TUI and Glasshouse web UI feel organized,
ergonomic, and discoverable for both first-time and power users by
restructuring the chrome, adding a searchable command palette, improving
feedback loops, and bringing the web UI to parity with the TUI.

## Principles

- **Organization first.** Every pane, tab, and sidebar section has one clear
  purpose. Hidden interactions (right-click, drag-only) get visible
  affordances.
- **Ergonomic commands.** Slash commands should be discoverable without
  memorizing `/help`. Context-aware hints and a searchable palette replace
  the modal help wall.
- **Clear feedback.** The organism’s background work (thinking, dreaming,
  listening, MUD turns) is always visible. Errors and pending mutations are
  hard to miss.
- **TUI leads, web follows.** The TUI is the priority surface. The web UI
  gets the same interaction model and feature set behind shared backend
  adapters.

---

## Section 1: TUI Layout Reorganization

### 1.1 Persistent header bar

- Always show organism name, lifecycle state (`wake`/`sleep`/`dead`), mood,
  and a compact voice-status indicator.
- Replace the current sparse top bar with a two-line header: organism
  identity on the left, global controls (`/voice`, `/listen`, `/look`,
  `/mud`) on the right.

### 1.2 Sidebar navigator

Restructure the sidebar into clearly labeled sections:

1. **This organism** — current organism, highlighted. One-click actions:
   rename, save, sleep/wake.
2. **Other organisms** — collapsible list; clicking swaps; drag-and-drop
   still works but each row also has a visible `⋯` menu.
3. **Groups** — collapsible; visible `+` button to create a group; group
   headers have a `⋯` menu for rename/delete.
4. **Quick actions** — buttons for `/new`, `/group start`, `/mud toggle`,
   `/reload`.

Remove the reliance on right-click and drag as the only discovery path.
Keep drag-and-drop as a shortcut for power users.

### 1.3 Visible tab bar

Add a labeled tab bar above the content area:

- Chat (F2)
- Mind (F3)
- Memory (F4)
- Inner (F7)
- Cells (F8)
- MUD (when active)

Tabs are clickable and keyboard-navigable. The active tab is visually
highlighted.

### 1.4 Bottom status bar

Simplify the status bar into two parts:

- Left: current activity (`idle`, `thinking…`, `dreaming…`, `listening…`,
  `MUD turn in 3s`).
- Right: compact counters (`beliefs`, `rules`, `voice: online/offline/?`).

### 1.5 MUD gets a dedicated tab

`/mud` opens the MUD tab instead of replacing the chat view. Layout:

- Left: story text and room description.
- Right: compact command reference and recent moves.
- Input still comes from the shared chat/command bar.

---

## Section 2: Command Ergonomics

### 2.1 Searchable command palette

- Bind `ctrl+p` / `F1` to a palette that lists all slash commands with
  usage and description.
- Typing filters the list. `Enter` fills the chat input with the selected
  command and closes the palette.
- Commands are grouped by category: State, Voice, Senses, MUD, Organisms,
  System, Help.

### 2.2 Inline input hints

- When the chat input starts with `/`, render a filtered list of commands
  and one-line descriptions directly above the input.
- When a command with subcommands is typed (e.g., `/voice `), show the
  valid subcommands: `on | off | list | use <name> | get <name>`.
- `Tab` cycles completions; `Esc` closes hints.

### 2.3 Sidebar action buttons

- Add one-click buttons in the sidebar for state changes:
  - Sleep / Wake
  - Voice on/off
  - Listen (push-to-talk)
  - Look (camera)
  - MUD toggle
- Each button updates its label to reflect state.

### 2.4 Confirm destructive actions

- `/revert`, organism deletion, MUD reset, and group deletion show a
  modal confirmation.
- Default focused button is “Cancel”; power users can `Enter` for the
  highlighted action after reading.

### 2.5 Searchable command history

- Keep existing up/down history browsing.
- Add `ctrl+r` to open a reverse-search palette over the per-organism
  history.

---

## Section 3: Feedback and Status Clarity

### 3.1 Global activity indicator

- Show a persistent indicator when the organism is working:
  - `musing…`
  - `reflecting…`
  - `listening…`
  - `MUD turn in Ns`
- Indicator is in the header bar or status bar and disappears when idle.

### 3.2 Structured system messages in chat

- Render lifecycle and system events as subtle, dim lines in the chat log:
  - `fern fell asleep`
  - `fern started dreaming`
  - `patch proposed: adjust kindness weighting`
  - `voice is offline — using fallback replies`
- These are not organism utterances; they are meta-log entries.

### 3.3 Pending-mutation banner

- When `extensions.pending` is non-None, show a sticky banner above the
  chat input with:
  - Patch kind and summary.
  - **Approve**, **Reject**, **Why?** buttons.
- Banner updates immediately after approval/rejection.

### 3.4 Toast-style errors

- Replace silent failures and any `alert()`-equivalents with brief,
  non-blocking toasts at the bottom of the screen:
  - `Camera not found`
  - `Voice download failed`
  - `No microphone matched`
  - `Invalid command: /foo`

### 3.5 Better empty states

- Mind tab: “No beliefs yet. Tell fern something about yourself.”
- Memory tab: “No memories yet. Memories form as you talk.”
- Inner tab: explanation of what the gauges mean.

---

## Section 4: Glasshouse Web UI Parity

### 4.1 Command bar

- Add a bottom input that accepts the same `/` commands as the TUI.
- Same inline hints and searchable palette (triggered by `/` or a `?`
  button).

### 4.2 Visible tab navigation

- Replace the current top nav with a tab bar matching the TUI:
  Habitat, Mind, Memory, Inner, Cells, MUD.
- Nursery panel stays as a collapsible sidebar section.

### 4.3 Stewardship panel expansion

Add the missing toggles and indicators:

- Voice on/off + voice selector.
- Listen (push-to-talk button).
- Look (camera snapshot button).
- Self-talk toggle.
- Auto-apply mutations toggle.
- Git sensing toggle.
- Activity indicator.

### 4.4 Mutation approval UI

- Same sticky banner as the TUI for pending patches.

### 4.5 Keyboard shortcuts

- `?` — help / command palette.
- `Esc` — close modals/palettes.
- `/` — focus command bar.
- `1`–`6` — switch tabs.

### 4.6 Responsive layout

- Collapse the three-column layout on narrow screens:
  - Header stays full-width.
  - Nursery and stewardship panels become top/bottom collapsible drawers.
  - Main content takes the full width.

---

## Files Likely to Change

- `src/replicanta/tui.py` — header, sidebar, tab bar, status bar, command
  palette, mutation banner, toasts.
- `src/replicanta/tui_commands.py` — command metadata grouped by category,
  palette search helpers.
- `src/replicanta/tui_views.py` — empty states, tab labels, activity
  renderables.
- `src/replicanta/web_static.py` — HTML/CSS/JS for command bar, tab bar,
  stewardship panel, responsive grid.
- `src/replicanta/web.py` — API endpoints for palette metadata, command
  execution, activity state.
- `tests/test_tui_*.py` — updated selectors and new tests for palette,
  sidebar, banner.
- `tests/test_web.py` — new tests for command bar and web parity.

## Out of Scope

- Full TUI rewrite or splitting `tui.py` into modules (covered by the
  existing `tui-hub-shard` desloppify cluster).
- Changing the organism’s cognitive pipeline or LLM prompts.
- Adding new commands beyond the existing `/` set.
- Real-time streaming of LLM tokens.
