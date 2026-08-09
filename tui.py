import random
from datetime import datetime, timezone
from typing import ClassVar

import narration
import tui_commands
import tui_views
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Matcher, Provider
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Header,
    Input,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)


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
STYLE_SELF = "italic yellow"
STYLE_WARN = "red"
STYLE_DIM = "dim"

NARRATE_INTERVAL = 45.0   # seconds between self-narrations (each = 5 LLM calls)
VOICE_PROBE_INTERVAL = 60.0   # seconds between ollama reachability probes
ASK_USER_ODDS = 0.35      # chance an idle wake utterance asks the user instead


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
        Binding("f2", "show_tab('chat-pane')", "chat"),
        Binding("f3", "show_tab('mind-pane')", "mind"),
        Binding("f4", "show_tab('memory-pane')", "memory"),
        Binding("ctrl+s", "save_now", "save"),
        Binding("ctrl+t", "think_now", "think"),
    ]

    CSS = """
    #status { height: 1; padding: 0 1; background: $surface; }
    TabbedContent { height: 1fr; }
    #dreams { height: 1fr; padding: 0 1; }
    #pending { height: auto; max-height: 4; padding: 0 1; color: green; }
    #mind, #memory { padding: 1 2; }
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
        self._self_talk_on = False
        self._self_talking = False
        self._rng = random.Random()
        self._last_was_question = False
        self._pending_text = ""
        self._pending_visible = False
        self._busy_frame = 0
        self._status_text = ""
        self._mind_text = ""
        self._memory_text = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="status")
        with TabbedContent(initial="chat-pane"):
            with TabPane("chat", id="chat-pane"):
                yield RichLog(
                    id="dreams", max_lines=1000, wrap=True, markup=True,
                    highlight=False)
                yield Static("", id="pending", markup=False)
            with TabPane("mind", id="mind-pane"):
                yield Static("", id="mind", markup=False)
            with TabPane("memory", id="memory-pane"):
                yield Static("", id="memory", markup=False)
        self.chat_input = Input(
            placeholder="talk to me, or /help …  (tab completes · "
                        "F2 chat · F3 mind · F4 memory)",
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
            self._log_chat(role, line, stamp=False)
        self.refresh_status()
        self.set_interval(1.0, self._on_tick)
        self.set_interval(NARRATE_INTERVAL, self._maybe_narrate)
        self.set_interval(VOICE_PROBE_INTERVAL, self._probe_voice)
        self._probe_voice()
        self._maybe_narrate()
        # start with the cursor in the chat line, not the scrollable log
        self.chat_input.focus()

    # -- actions ---------------------------------------------------------
    def action_show_tab(self, pane):
        self.query_one(TabbedContent).active = pane

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
                self.notify("voice offline — local fallback",
                            severity="warning")
            elif state == "online":
                self._append_log("voice: online (ollama)", STYLE_DIM)
                self.notify("voice online (ollama)")
        self.refresh_status()

    # -- ticks -----------------------------------------------------------
    def _on_tick(self):
        for event in self.org.tick(1.0):
            self._render_event(event)
        if self._busy():
            self._busy_frame = (self._busy_frame + 1) % 3
        self._refresh_views()
        self.refresh_status()

    def _busy(self):
        return (self._narrating or self._responding or self._self_talking)

    def _refresh_views(self):
        self._mind_text = tui_views.mind_view(self.org)
        self._memory_text = tui_views.memory_view(self.org)
        self.query_one("#mind", Static).update(self._mind_text)
        self.query_one("#memory", Static).update(self._memory_text)

    def _render_event(self, event):
        """Render one engine event into the log."""
        kind = event["kind"]
        if kind == "state":
            to = event["to"]
            if to == "dead":
                self._append_log("the organism has faded.", STYLE_WARN,
                                 stamp=True)
                self.notify("the organism has faded", severity="error")
                self._maybe_narrate()
            else:
                self._append_log(
                    f"— the organism drifts to {to} —", STYLE_DIM,
                    stamp=True)
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
        elif kind == "mood":
            mood = event["mood"]
            style = (STYLE_WARN if mood in ("hurt", "anxious")
                     else STYLE_LEARNED if mood in ("grateful", "curious")
                     else STYLE_DIM)
            self._append_log(f"mood: {mood}", style)
        elif kind == "learned":
            self._append_log(f"learned: {event['text']}", STYLE_LEARNED,
                             stamp=True)
            self.notify(f"learned: {event['text']}")

    def refresh_status(self):
        lc = self.org.lifecycle
        icon = {"wake": "🧠", "sleep": "💤", "dead": "🪦"}.get(lc.state, "🧠")
        word = {"wake": "awake", "sleep": "asleep",
                "dead": "faded"}.get(lc.state, lc.state)
        mood = next(
            (v for (o, a, v) in self.org.store.beliefs()
             if (o, a) == ("self", "mood")), "—")
        m = self.org.metrics()
        busy = (f" · thinking{'.' * (self._busy_frame + 1)}"
                if self._busy() else "")
        self._status_text = (
            f"{icon} {word} · {mood} · {m.belief_count} beliefs · "
            f"{m.rule_count} rules · voice {narration.voice_status()} · "
            f"{self.org.probe.clock_utc()}{busy}")
        self.query_one("#status", Static).update(self._status_text)

    # -- log ---------------------------------------------------------------
    def _stamp(self):
        return datetime.now(timezone.utc).astimezone().strftime("%H:%M")

    def _append_log(self, text, style=None, stamp=False):
        """Append one styled line to the scrollable log (markup-escaped),
        optionally prefixed with a dim HH:MM timestamp."""
        line = escape(text)
        if style:
            line = f"[{style}]{line}[/{style}]"
        if stamp:
            line = f"[dim]{self._stamp()}[/dim] {line}"
        self.query_one("#dreams", RichLog).write(line)

    def _log_chat(self, role, text, stamp=True):
        if role == "user":
            self._write_card("you", text, STYLE_YOU, stamp=stamp)
        else:
            self._write_card("org", text, STYLE_ORG, stamp=stamp)

    def _write_card(self, who, text, border_style, stamp=True):
        """One conversation message as a padded card (role-colored border,
        timestamped title), preceded by a blank line so exchanges breathe.
        Content is a plain Rich Text — organism output may contain markup
        metacharacters."""
        title = f"{who} · {self._stamp()}" if stamp else who
        card = Panel(Text(text), title=title, title_align="left",
                     border_style=border_style, padding=(0, 1))
        log = self.query_one("#dreams", RichLog)
        log.write("")
        log.write(card)

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
        if (self.org.lifecycle.state == "wake"
                and not self._last_was_question
                and self._rng.random() < ASK_USER_ODDS):
            self._last_was_question = True
            self.refresh_status()
            self._ask_user()
            return
        self._last_was_question = False
        self.refresh_status()
        self._narrate()

    @work(thread=True)
    def _ask_user(self):
        question = None
        try:
            self.call_from_thread(self._pending_show, "org is wondering")
            question = narration.ask_user(
                self.org,
                on_token=lambda tok: self.call_from_thread(
                    self._pending_token, tok))
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "question", exc)
        finally:
            self._narrating = False
        if question is not None:
            self.call_from_thread(self._set_user_question, question)

    def _set_user_question(self, question):
        self._pending_hide()
        self.org.store.record_chat("org", question)
        self._write_card("org", question, STYLE_ORG)
        self.refresh_status()

    # -- self-talk ---------------------------------------------------------
    def _maybe_self_talk(self):
        if not self._self_talking:
            self._self_talking = True
            self.refresh_status()
            self._self_talk()

    @work(thread=True)
    def _self_talk(self):
        answer = None
        try:
            self.call_from_thread(
                self._pending_show, "org is asking itself")
            question = narration.self_ask(self.org)
            self.call_from_thread(self._pending_hide)
            self.call_from_thread(self._set_self_question, question)
            self.call_from_thread(self._pending_show, "org is answering")
            answer = narration.self_answer(
                self.org, question,
                on_token=lambda tok: self.call_from_thread(
                    self._pending_token, tok))
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "self-talk", exc)
        finally:
            self._self_talking = False
        if answer is not None:
            self.call_from_thread(self._set_self_answer, answer)

    def _set_self_question(self, question):
        self.org.store.record_chat("org", question)
        self._write_card("self", question, "dim yellow")

    def _set_self_answer(self, answer):
        self._pending_hide()
        self.org.store.record_chat("org", answer)
        # nested under its question so the exchange reads as a dialogue
        self._append_log(f"  ↳ {answer}", STYLE_SELF)
        self.refresh_status()

    @work(thread=True)
    def _narrate(self):
        text = None
        try:
            self.call_from_thread(self._pending_show, "org is musing")
            text = narration.narrate(self.org)
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "narration", exc)
        finally:
            self._narrating = False
        if text is not None:
            self.call_from_thread(self._log_narration, text)

    def _log_narration(self, text):
        self._pending_hide()
        self._write_card("org", text, STYLE_ORG)
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
        elif name == "/self-talk":
            self._self_talk_on = not self._self_talk_on
            if self._self_talk_on:
                self._append_log(
                    "self-talk on — the organism may speak to itself.",
                    STYLE_DIM)
                if self.org.lifecycle.state == "wake":
                    self._maybe_self_talk()
            else:
                self._append_log("self-talk off", STYLE_DIM)
        elif name == "/help":
            self.action_help()
        else:
            self._append_log(f"unknown: {name} (try /help)", STYLE_WARN)

    def handle_chat(self, text):
        self._log_chat("user", text)
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
        reply = None
        try:
            self.call_from_thread(self._pending_show, "org is thinking")
            reply = narration.respond(
                self.org, text,
                on_token=lambda tok: self.call_from_thread(
                    self._pending_token, tok))
        except Exception as exc:  # noqa: BLE001 — workers must never die silently
            self.call_from_thread(self._worker_error, "reply", exc)
        finally:
            self._responding = False
        if reply is not None:
            self.call_from_thread(self._set_reply, reply)

    def _set_reply(self, reply):
        self._pending_hide()
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
