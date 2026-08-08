from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Static


class OrganismApp(App):
    """Terminal front-end: chat line, status pane, dream stream, belief
    viewer. Commands: /chaos N, /focus X, /sleep, /wake, /stats, /save."""

    CSS = """
    Screen { layout: horizontal; }
    #left { width: 40%; height: 100%; border: solid green; }
    #right { width: 60%; height: 100%; border: solid blue; }
    #chat { height: 3; border: solid yellow; }
    #dreams { height: 40%; border: solid magenta; }
    #beliefs { height: 57%; border: solid cyan; overflow-y: auto; }
    """

    def __init__(self, organism):
        super().__init__()
        self.org = organism

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield Static("STATUS", id="status")
                yield Static("DREAMS", id="dreams")
            with Vertical(id="right"):
                yield Static("BELIEFS", id="beliefs")
                yield Input(placeholder="talk to me, or /chaos 0.7 ...", id="chat")
        yield Footer()

    def on_mount(self):
        self.refresh_status()
        self.refresh_beliefs()
        self.set_interval(1.0, self._on_tick)

    def _on_tick(self):
        self.org.mind.rebuild()
        if self.org.lifecycle.due():
            if self.org.lifecycle.state == "wake":
                self.org.lifecycle._transition("sleep")
                promoted = self.org._sleep()
                if promoted:
                    self.query_one("#dreams", Static).update(
                        "DREAM: " + ", ".join(p["combo"] for p in promoted))
                else:
                    self.query_one("#dreams", Static).update("DREAMS: (none promoted)")
            else:
                self.org.lifecycle._transition("wake")
                self.org._wake()
        self.refresh_status()
        self.refresh_beliefs()

    def refresh_status(self):
        m = self.org.metrics()
        self.query_one("#status", Static).update(
            f"state: {self.org.lifecycle.state} | cycle: {self.org.store.cycle} "
            f"| chaos: {self.org.store.chaos:.2f} | beliefs: {m.belief_count} "
            f"| rules: {m.rule_count} | score: {m.score():.1f}")

    def refresh_beliefs(self):
        lines = [f"{conf:.2f}  {obj}:{attr}={val}"
                 for (obj, attr, val), conf
                 in sorted(self.org.store.beliefs().items())]
        self.query_one("#beliefs", Static).update("\n".join(lines[-40:]))

    def on_input_submitted(self, event):
        text = event.value.strip()
        self.query_one("#chat", Input).value = ""
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
        elif name == "/stats":
            m = self.org.metrics()
            self.query_one("#dreams", Static).update(
                f"stats: beliefs={m.belief_count} rules={m.rule_count} "
                f"depth={m.total_depth} score={m.score():.1f}")
        elif name == "/save":
            self.org.store.save()

    def handle_chat(self, text):
        self.query_one("#dreams", Static).update(f"you: {text}")


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
