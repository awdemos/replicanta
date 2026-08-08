from typing import ClassVar

import narration
import tui_commands
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Matcher, Provider
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Static


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


class OrganismApp(App):
    """Terminal front-end for the Scallop organism: mind pane with LLM
    narration + live activity, dream stream, belief viewer, command line
    with tab completion. Commands: /chaos N, /focus X, /sleep, /wake,
    /stats, /save, /think, /help (or ctrl+p / F1)."""

    COMMANDS: ClassVar[set] = App.COMMANDS | {SlashCommands}
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+p", "command_palette", "command palette"),
        Binding("f1", "help", "help"),
        Binding("ctrl+s", "save_now", "save"),
        Binding("ctrl+t", "think_now", "think"),
    ]

    CSS = """
    Screen { layout: horizontal; }
    #left { width: 40%; height: 100%; border: solid green; }
    #right { width: 60%; height: 100%; border: solid blue; }
    #chat { height: 3; border: solid yellow; }
    #mind { height: 42%; border: solid magenta; }
    #dreams { height: 22%; border: solid cyan; }
    #beliefs { border: solid cyan; overflow-y: auto; }
    #help { border: round green; padding: 1 2; width: 60; height: auto; }
    """

    def __init__(self, organism):
        super().__init__()
        self.org = organism
        self.chat_input = None
        self._narration = None
        self._narrating = False
        self._responding = False
        self._last_beliefs = 0
        self._last_rules = 0
        self._history = []
        self._completion_index = 0
        self._chat_history = []
        self._history_index = -1
        self._history_draft = ""
        self._suppress_changed = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield Static("STATUS", id="status")
                yield Static("🧠 MIND", id="mind")
                yield Static("DREAMS", id="dreams")
            with Vertical(id="right"):
                yield Static("BELIEFS", id="beliefs")
                self.chat_input = Input(
                    placeholder="talk to me, or /chaos 0.7 ... (tab completes)",
                    id="chat")
                yield self.chat_input
        yield Footer()

    def on_mount(self):
        m = self.org.metrics()
        self._last_beliefs = m.belief_count
        self._last_rules = m.rule_count
        self._chat_history = [line for role, line in self.org.store.chat_log
                              if role == "user"]
        self.refresh_status()
        self.refresh_beliefs()
        self.refresh_mind()
        self.set_interval(1.0, self._on_tick)
        self.set_interval(15.0, self._maybe_narrate)
        self._maybe_narrate()

    # -- actions ---------------------------------------------------------
    def action_help(self):
        self.push_screen(HelpScreen())

    def action_save_now(self):
        self.org.store.save()

    def action_think_now(self):
        self._narration = "thinking…"
        self.refresh_mind()
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

    # -- ticks -----------------------------------------------------------
    def _on_tick(self):
        self.org.sense()
        self.org.mind.rebuild()
        self.org.meter.tick(
            sleeping=(self.org.lifecycle.state == "sleep"), dt=1.0)
        new_state = self.org.lifecycle.advance()
        if new_state == "sleep":
            promoted = self.org._sleep()
            if promoted:
                self.query_one("#dreams", Static).update(
                    "DREAM: " + ", ".join(p["combo"] for p in promoted))
            else:
                self.query_one("#dreams", Static).update(
                    "DREAMS: (none promoted)")
        elif new_state == "wake":
            self.org._wake()
        elif new_state == "dead":
            self.query_one("#dreams", Static).update(
                "the organism has faded.")
            self._narration = "…fading, gently, into the quiet."
            self._maybe_narrate()
        self.refresh_status()
        self.refresh_beliefs()
        self.refresh_mind()

    def refresh_status(self):
        m = self.org.metrics()
        self.query_one("#status", Static).update(
            f"state: {self.org.lifecycle.state} | cycle: {self.org.store.cycle} "
            f"| chaos: {self.org.store.chaos:.2f} | stress: {self.org.store.stress:.2f} "
            f"| beliefs: {m.belief_count} "
            f"| rules: {m.rule_count} | score: {m.score():.1f}")

    def refresh_beliefs(self):
        lines = [f"{conf:.2f}  {obj}:{attr}={val}"
                 for (obj, attr, val), conf
                 in sorted(self.org.store.beliefs().items())]
        self.query_one("#beliefs", Static).update("\n".join(lines[-40:]))

    def refresh_mind(self):
        m = self.org.metrics()
        delta_b = m.belief_count - self._last_beliefs
        delta_r = m.rule_count - self._last_rules
        self._last_beliefs = m.belief_count
        self._last_rules = m.rule_count
        self._history.append(m.belief_count)
        if len(self._history) > 24:
            self._history.pop(0)
        lc = self.org.lifecycle
        limit = (lc.wake_seconds if lc.state == "wake"
                 else lc.sleep_seconds if lc.state == "sleep" else 1)
        frac = min(1.0, lc.elapsed() / limit) if limit else 0.0
        bar = "█" * int(frac * 10) + "░" * (10 - int(frac * 10))
        window = ", ".join(sorted(str(p) for p in self.org.window.pairs)) or "—"
        thought = " ".join((self._narration or "thinking…").splitlines())
        icon = {"wake": "🧠", "sleep": "💤", "dead": "🪦"}.get(lc.state, "🧠")
        self.query_one("#mind", Static).update(
            f"{icon} {lc.state} | cycle {self.org.store.cycle} "
            f"| chaos {self.org.store.chaos:.2f} | stress {self.org.store.stress:.2f} "
            f"| {bar}\n"
            f"beliefs {m.belief_count} (+{delta_b}) "
            f"| rules {m.rule_count} (+{delta_r}) "
            f"| score {m.score():.1f}\n"
            f"activity {tui_commands.sparkline(self._history)}\n"
            f"window {window}\n"
            f"thought: {thought}")

    # -- narration -------------------------------------------------------
    def _maybe_narrate(self):
        if not self._narrating:
            self._narrating = True
            self._narrate()

    @work(thread=True)
    def _narrate(self):
        try:
            text = narration.narrate(self.org)
        finally:
            self._narrating = False
        self.call_from_thread(self._set_narration, text)

    def _set_narration(self, text):
        self._narration = text
        self.refresh_mind()

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
            if self.org.lifecycle.state == "wake":
                self.org.lifecycle._transition("sleep")
                self.org._sleep()
        elif name == "/wake":
            if self.org.lifecycle.state == "sleep":
                self.org.lifecycle._transition("wake")
                self.org._wake()
        elif name == "/revive":
            if self.org.lifecycle.state == "dead":
                self.org.lifecycle.revive()
                self.org.store.save()
                self.query_one("#dreams", Static).update(
                    "revived: the organism stirs back into existence.")
                self._maybe_narrate()
            else:
                self.query_one("#dreams", Static).update(
                    f"/revive: it is not faded (state "
                    f"{self.org.lifecycle.state}).")
        elif name == "/stats":
            m = self.org.metrics()
            self.query_one("#dreams", Static).update(
                f"stats: beliefs={m.belief_count} rules={m.rule_count} "
                f"depth={m.total_depth} score={m.score():.1f}")
        elif name == "/save":
            self.org.store.save()
        elif name == "/think":
            self.action_think_now()
        elif name == "/help":
            self.action_help()
        else:
            self.query_one("#dreams", Static).update(
                f"unknown: {name} (try /help)")

    def handle_chat(self, text):
        self.org.store.record_chat("user", text)
        self.query_one("#dreams", Static).update(f"you: {text}")
        self.org.meter.bump(tui_commands.harshness(text))
        self._maybe_respond(text)


    def _maybe_respond(self, text):
        if not self._responding:
            self._responding = True
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
        self._set_narration(reply)


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
