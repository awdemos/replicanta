"""Hooks: a user-scriptable interface for the organism. Lua scripts in
`scripts/*.lua` (at the nursery root, or beside the organism for
standalone dirs) define event functions — on_birth, on_cycle, on_learned,
on_utterance, on_fade — and the engine calls them at the corresponding
moments. Each receives a ctx table:

    ctx.event, ctx.text        -- what happened (+ the words, when any)
    ctx.state, ctx.cycle       -- wake/sleep/dead, lifecycle cycle
    ctx.mood                   -- current mood belief
    ctx.belief_count, ctx.rule_count, ctx.score
    ctx.chaos, ctx.stress
    ctx.organism               -- the organism's nursery dir name
    ctx.activity               -- neurosymbolic activity counters (table)

and safe actuators:

    ctx.log(msg)               -- append a line to the chat log
    ctx.set_chaos(x)           -- retune randomness (clamped to 0..1)
    ctx.focus(attr)            -- steer attention (nil to clear)

Scripts are sandboxed (no os/io/require/load), every hook call is
protected (a broken script logs an error line, never kills the
organism), and a lock makes the single Lua runtime safe against the
TUI's worker threads. `/reload` re-reads the scripts directory.
`/lua name.lua` runs one script on demand: it is executed in the same
sandbox and its `main(ctx)` (when defined) is called with ctx.event set
to "lua"."""

import threading
from pathlib import Path

EVENTS = ("birth", "cycle", "learned", "utterance", "fade")

_BLOCKED_GLOBALS = ("os", "io", "load", "loadstring", "require", "dofile")


class HookEngine:
    """Discovers and fires Lua hooks. Pure apart from the `emit` callback
    (which the TUI points at the chat log); headless organisms work too."""

    def __init__(self, scripts_dir, emit=None):
        self.scripts_dir = Path(scripts_dir)
        self.emit = emit if emit is not None else (lambda _msg: None)
        self._lock = threading.Lock()
        self._lua = None
        self._available = None   # None = untested, False = lupa missing
        self.reload()

    def reload(self):
        """Re-read the scripts directory (drop + rebuild the runtime)."""
        self.scripts = (sorted(self.scripts_dir.glob("*.lua"))
                        if self.scripts_dir.is_dir() else [])
        self._lua = None

    # -- runtime -----------------------------------------------------------
    def _runtime(self):
        from lupa import LuaRuntime
        lua = LuaRuntime(register_eval=False, register_builtins=False)
        for name in _BLOCKED_GLOBALS:
            lua.execute(f"{name} = nil")
        return lua

    def _ctx(self, org, event, text):
        m = org.metrics()
        mood = next((v for (o, a, v) in org.store.beliefs()
                     if (o, a) == ("self", "mood")), "calm")
        return self._lua.table(
            event=event,
            text=text,
            state=org.lifecycle.state,
            cycle=org.store.cycle,
            mood=mood,
            belief_count=m.belief_count,
            rule_count=m.rule_count,
            score=m.score(),
            chaos=org.store.chaos,
            stress=org.store.stress,
            arousal=org.store.arousal,
            rationality=org.store.rationality,
            irrationality=org.store.irrationality,
            insane=org.store.insane,
            organism=org.dir_path.name,
            activity=self._lua.table_from(dict(org.store.activity)),
            log=lambda msg: self.emit(str(msg)),
            set_chaos=lambda x: self._set_chaos(org, x),
            focus=lambda attr: self._focus(org, attr),
        )

    @staticmethod
    def _set_chaos(org, x):
        org.store.chaos = max(0.0, min(1.0, float(x)))

    @staticmethod
    def _focus(org, attr):
        org.window.focus(attr if attr else None)
        org.store.attention = org.window.pairs

    # -- firing --------------------------------------------------------------
    def fire(self, event, org, text=None):
        """Call on_<event>(ctx) in every script. Never raises."""
        if not self.scripts or event not in EVENTS:
            return
        with self._lock:
            if self._available is False:
                return
            try:
                if self._lua is None:
                    self._lua = self._runtime()
                    self._available = True
            except ImportError:
                self._available = False
                self.emit("lua hooks disabled: the 'lupa' package "
                          "is not installed")
                return
            ctx = self._ctx(org, event, text)
            for script in self.scripts:
                try:
                    self._lua.execute(script.read_text())
                    hook = self._lua.globals()[f"on_{event}"]
                    if hook is not None:
                        hook(ctx)
                except Exception as exc:  # noqa: BLE001 — user scripts must never kill the organism
                    self.emit(f"{script.name}: {exc}")

    def run(self, name, org):
        """Run one named script on demand (the /lua command): execute it in
        the shared sandbox, then call its main(ctx) when defined. Returns
        a status line for the chat log; never raises."""
        if Path(name).name != name or not name.endswith(".lua"):
            return f"/lua: bad script name {name!r} (want a plain *.lua file)"
        script = self.scripts_dir / name
        if not script.is_file():
            return f"/lua: no {name} in {self.scripts_dir}"
        with self._lock:
            if self._available is False:
                return "lua hooks disabled: the 'lupa' package is not installed"
            try:
                if self._lua is None:
                    self._lua = self._runtime()
                    self._available = True
            except ImportError:
                self._available = False
                return ("lua hooks disabled: the 'lupa' package "
                        "is not installed")
            try:
                self._lua.execute(script.read_text())
                main = self._lua.globals()["main"]
                if main is not None:
                    main(self._ctx(org, "lua", name))
                return f"lua: ran {name}"
            except Exception as exc:  # noqa: BLE001 — user scripts must never kill the organism
                return f"{name}: {exc}"


def scripts_dir_for(dir_path):
    """Where an organism's hooks live: the nursery root's scripts/ when the
    organism is in a nursery (organisms/<name>/), else beside it."""
    dir_path = Path(dir_path)
    if dir_path.parent.name == "organisms":
        return dir_path.parent.parent / "scripts"
    return dir_path / "scripts"
