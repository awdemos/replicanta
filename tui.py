from typing import ClassVar

import narration
import tui_commands
from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Matcher, Provider
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, RichLog, Static


class SlashCommands(Provider):
    """Feeds the command palette (ctrl+p) with the slash commands; chosen
    entries fill the chat line and run it."""

    def _run(self, usage):
        self.app.chat_input.value = usage
        self.app.chat_input.focus()
        self.app.chat_input.action_submit()

    def _hit(self, name, usage, description, score=1.0, display=None):
        return Hit(
            score=score,
            match_display=display or f"{name}  {description}",
            command=lambda u=usage: self._run(u),
            help=description,
        )

    async def discover(self):
        for name, usage, description in tui_commands.COMMANDS:
            yield self._hit(name, usage, description)

    async def search(self, query):
        matcher = Matcher(query)
        for name, usage, description in tui_commands.COMMANDS:
            match = matcher.match(f"{name} {description}")
            if match is not None:
                yield self._hit(name, usage, description,
                                score=match.score, display=match.highlight)


class HelpScreen(ModalScreen):
    """Overlay with every slash command and key binding."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss", "close")]

    def compose(self) -> ComposeResult:
        yield Static(tui_commands.help_text(), id="help")


# role -> log style (Rich markup); engine events get their own styles
STYLE_YOU = "cyan"
STYLE_ORG = "green"
STYLE_DREAM = "magenta"
STYLE_LEARNED = "yellow"
STYLE_WARN = "red"
STYLE_DIM = "dim"

NARRATE_INTERVAL = 45.0   # seconds between self-narrations (each = 5 LLM calls)
VOICE_PROBE_INTERVAL = 60.0   # seconds between ollama reachability probes


class OrganismApp(App):
    """Terminal front-end for the Scallop organism, conversation-first: one
    dominant styled log (chat, dreams, learned facts, lifecycle events), a
    one-line status bar, and a command line with tab completion. Commands:
    /chaos N, /focus X, /sleep, /wake, /revive, /stats, /save, /think,
    /help (or ctrl+p / F1)."""

    COMMANDS: ClassVar[set] = App.COMMANDS | {SlashCommands}
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+p", "command_palette", "command palette"),
        Binding("f1", "help", "help"),
        Binding("ctrl+s", "save_now", "save"),
        Binding("ctrl+t", "think_now", "think"),
    ]

    CSS = """
    #status { height: 1; padding: 0 1; background: $surface; }
    #dreams { height: 1fr; border: solid cyan; padding: 0 1; }
    #chat { height: 3; border: solid yellow; }
    #help { border: round green; padding: 1 2; width: 60; height: auto; }
    """

    def __init__(self, organism):
        super().__init__()
        self.org = organism
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

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="status")
        yield RichLog(
            id="dreams", max_lines=1000, wrap=True, markup=True,
            highlight=False)
        self.chat_input = Input(
            placeholder="talk to me, or /chaos 0.7 ... (tab completes)",
            id="chat")
        yield self.chat_input
        yield Footer()

    def on_mount(self):
        self._chat_history = [line for role, line in self.org.store.chat_log
                              if role == "user"]
        if not self.org.store.chat_log and self.org.store.cycle == 0:
            self._append_log(
                "a tiny organism wakes up inside your machine.", STYLE_DIM)
            self._append_log(
                "talk to it — it learns from you. /help (or F1) for commands.",
                STYLE_DIM)
        for role, line in self.org.store.chat_log[-100:]:
            self._log_chat(role, line)
        self.refresh_status()
        self.set_interval(1.0, self._on_tick)
        self.set_interval(NARRATE_INTERVAL, self._maybe_narrate)
        self.set_interval(VOICE_PROBE_INTERVAL, self._probe_voice)
        self._probe_voice()
        self._maybe_narrate()

    # -- actions ---------------------------------------------------------
    def action_help(self):
        self.push_screen(HelpScreen())

    def action_save_now(self):
        self.org.flush(force=True)

    def action_think_now(self):
        self._maybe_narrate()

    # -- keys ------------------------------------------------------------
    def on_key(self, event):
        if self.chat_input is None or not self.chat_input.has_focus:
            return
        if event.key == "tab":
            value = self.chat_input.value
            new_value, self._completion_index = tui_commands.complete_command(
                value, self._completion_index)
            if new_value != value:
                self._set_chat_value(new_value)
            event.stop()
        elif event.key in ("up", "down"):
            delta = -1 if event.key == "up" else 1
            value = self._browse_history(delta)
            if value is not None and value != self.chat_input.value:
                self._set_chat_value(value)
            event.stop()

    def _set_chat_value(self, text):
        """Programmatic input set: value + cursor, suppressing the next
        Input.Changed so completion/history navigation state survives."""
        self._suppress_changed = True
        self.chat_input.value = text
        self.chat_input.cursor_position = len(text)

    def _browse_history(self, delta):
        self._history_index, self._history_draft, value = (
            tui_commands.history_browse(
                self._chat_history, self._history_index, self._history_draft,
                self.chat_input.value, delta))
        return value

    def on_input_changed(self, event):
        if not self._suppress_changed:
            self._completion_index = 0
            self._history_index = -1
        self._suppress_changed = False

    # -- voice health ------------------------------------------------------
    def _probe_voice(self):
        """Probe ollama reachability off the UI thread (noop while one is
        already in flight); the arena reads the cached result."""
        if not self._probing_voice:
            self._probing_voice = True
            self._probe_voice_worker()

    @work(thread=True)
    def _probe_voice_worker(self):
        try:
            narration.probe_voice()
        finally:
            self._probing_voice = False
        self.call_from_thread(self._announce_voice)

    def _announce_voice(self):
        """Tell the user once per voice-state flip how the organism speaks."""
        state = narration.voice_status()
        if state != self._voice_announced:
            self._voice_announced = state
            if state == "offline":
                self._append_log(
                    "voice: offline — speaking from my bones "
                    "(local fallback)", STYLE_DIM)
            elif state == "online":
                self._append_log("voice: online (ollama)", STYLE_DIM)
        self.refresh_status()

    # -- ticks -----------------------------------------------------------
    def _on_tick(self):
        for event in self.org.tick(1.0):
            self._render_event(event)
        self.refresh_status()

    def _render_event(self, event):
        """Render one engine event into the log."""
        kind = event["kind"]
        if kind == "state":
            to = event["to"]
            if to == "dead":
                self._append_log("the organism has faded.", STYLE_WARN)
                self._maybe_narrate()
            else:
                self._append_log(
                    f"— the organism drifts to {to} —", STYLE_DIM)
        elif kind == "dream":
            combos = event["combos"]
            if combos:
                self._append_log("dream: " + ", ".join(combos), STYLE_DREAM)
            else:
                self._append_log("dreams: (none promoted)", STYLE_DIM)
        elif kind == "beliefs":
            learned = ", ".join(
                f"{o}:{a}={v}" for (o, a, v) in event["new"])
            self._append_log(f"new beliefs: {learned}", STYLE_LEARNED)
        elif kind == "sense":
            self._append_log(
                f"the host strains (distress +{event['distress']:.2f})",
                STYLE_WARN)
        elif kind == "stress":
            level = "high" if event["band"] == 1 else "critical"
            self._append_log(f"stress rising: {level}", STYLE_WARN)

    def refresh_status(self):
        lc = self.org.lifecycle
        icon = {"wake": "🧠", "sleep": "💤", "dead": "🪦"}.get(lc.state, "🧠")
        mood = next(
            (v for (o, a, v) in self.org.store.beliefs()
             if (o, a) == ("self", "mood")), "—")
        busy = " | thinking…" if (self._narrating or self._responding) else ""
        self.query_one("#status", Static).update(
            f"{icon} {lc.state} | cycle {self.org.store.cycle} "
            f"| chaos {self.org.store.chaos:.2f} "
            f"| stress {self.org.store.stress:.2f} | mood {mood} "
            f"| beliefs {self.org.metrics().belief_count} "
            f"| voice {narration.voice_status()} "
            f"| {self.org.probe.clock_utc()}{busy}")

    # -- log ---------------------------------------------------------------
    def _append_log(self, text, style=None):
        """Append one styled line to the scrollable log (markup-escaped)."""
        line = escape(text)
        if style:
            line = f"[{style}]{line}[/{style}]"
        self.query_one("#dreams", RichLog).write(line)

    def _log_chat(self, role, text):
        style = STYLE_YOU if role == "user" else STYLE_ORG
        who = "you" if role == "user" else "org"
        self._append_log(f"{who}: {text}", style)

    # -- narration -------------------------------------------------------
    def _maybe_narrate(self):
        if not self._narrating:
            self._narrating = True
            self.refresh_status()
            self._narrate()

    @work(thread=True)
    def _narrate(self):
        try:
            text = narration.narrate(self.org)
        finally:
            self._narrating = False
        self.call_from_thread(self._log_narration, text)

    def _log_narration(self, text):
        self._append_log(f"org: {text}", STYLE_ORG)
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
        parts = cmd.split()
        name = parts[0]
        if name == "/chaos" and len(parts) == 2:
            self.org.store.chaos = float(parts[1])
        elif name == "/focus" and len(parts) == 2:
            self.org.window.focus(parts[1])
            self.org.store.attention = self.org.window.pairs
        elif name == "/focus":
            self.org.window.focus(None)
        elif name == "/sleep":
            for event in self.org.force_state("sleep"):
                self._render_event(event)
        elif name == "/wake":
            for event in self.org.force_state("wake"):
                self._render_event(event)
        elif name == "/revive":
            if self.org.revive():
                self._append_log(
                    "revived: the organism stirs back into existence.",
                    STYLE_DIM)
                self._maybe_narrate()
            else:
                self._append_log(
                    f"/revive: it is not faded (state "
                    f"{self.org.lifecycle.state}).", STYLE_DIM)
        elif name == "/stats":
            m = self.org.metrics()
            self._append_log(
                f"stats: beliefs={m.belief_count} rules={m.rule_count} "
                f"depth={m.total_depth} score={m.score():.1f}", STYLE_DIM)
        elif name == "/save":
            self.action_save_now()
        elif name == "/think":
            self.action_think_now()
        elif name == "/help":
            self.action_help()
        else:
            self._append_log(f"unknown: {name} (try /help)", STYLE_WARN)

    def handle_chat(self, text):
        self.org.store.record_chat("user", text)
        self._log_chat("user", text)
        self.org.meter.bump(tui_commands.harshness(text))
        self._maybe_respond(text)

    def _maybe_respond(self, text):
        if not self._responding:
            self._responding = True
            self.refresh_status()
            self._respond(text)

    @work(thread=True)
    def _respond(self, text):
        try:
            reply = narration.respond(self.org, text)
        finally:
            self._responding = False
        self.call_from_thread(self._set_reply, reply)

    def _set_reply(self, reply):
        self.org.store.record_chat("org", reply)
        self._log_chat("org", reply)
        self.refresh_status()


def main():
    import argparse
    from pathlib import Path

    from organism import Organism
    parser = argparse.ArgumentParser(description="Scallop Organism TUI")
    parser.add_argument("--dir", default=str(Path(__file__).parent))
    parser.add_argument("--wake", type=int, default=180)
    parser.add_argument("--sleep", type=int, default=60)
    parser.add_argument("--chaos", type=float, default=0.5)
    args = parser.parse_args()
    org = Organism(Path(args.dir), wake_seconds=args.wake,
                   sleep_seconds=args.sleep, chaos=args.chaos)
    org.load()
    OrganismApp(org).run()


if __name__ == "__main__":
    main()
