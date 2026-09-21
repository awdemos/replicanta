"""Fly-brain bridge service for Replicanta organisms.

Exposes the ``rsi-wetware-rs`` binary (a real larval Drosophila connectome
running as a reservoir computer, wrapped in a recursive self-improvement
loop) to Lua modules as the ``flybrain`` service. This stays a thin bridge:
table-in/plain-data-out, one subprocess per call, and async completion that
re-enters Lua under the host's lua_lock — the same contract as tendon_hand.
"""

import json
import logging
import os
import subprocess
import threading
from pathlib import Path

from replicanta import lua_sandbox

log = logging.getLogger(__name__)

TASKS = ("digits", "timeseries")
DEFAULT_TIMEOUT = 1800.0
QUICK_TIMEOUT = 120.0


class FlyBrainService:
    """Subprocess bridge to the ``wetware`` CLI.

    Exposed to Lua as ``services.get('flybrain')`` with methods:
      path(), available(), status(), info(sample), bank(),
      run_task(task, sample, wait, on_done),
      optimize(task, sample, wait, on_done),
      adapt(on_done, seed, sample, wait), running(), last().

    ``run_task`` runs the plain ``run`` harness and is the fallback for
    minimal ``wetware-rs`` binaries that have no recursive-self-improvement
    loop; ``optimize`` is a thin compatibility alias for prompts written
    against the older loop; ``adapt`` runs the L4 drift-rehearsal demo.
    All three share one contract: with ``wait=True`` they block and
    return the parsed report as a DictProxy; by default they run async,
    which requires an ``on_done`` callback (a Lua function invoked under
    ``lua_lock`` when the subprocess finishes) and returns a status table
    ``{ok=true, async=true, kind=...}``. A missing ``on_done`` raises
    ValueError and an in-flight run raises RuntimeError.
    """

    def __init__(self, root=None, lua_lock=None, binary=None, timeout=DEFAULT_TIMEOUT):
        self._root = Path(root) if root is not None else None
        self._lua_lock = lua_lock if lua_lock is not None else threading.Lock()
        self._binary = binary or os.environ.get("WETWARE_BIN")
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self._running = False
        self._last = None  # (ok, summary_text, report dict | None)

    # -- binary resolution -----------------------------------------------------
    def _candidates(self):
        if self._binary:
            yield Path(self._binary).expanduser()
            return
        bases = []
        if self._root is not None:
            bases.append(Path(self._root).parent)
        bases.append(Path.home() / "code")
        seen = set()
        # Prefer the richer rsi-wetware-rs binary over the minimal wetware-rs one.
        for base in bases:
            for profile in ("release", "debug"):
                for repo in ("rsi-wetware-rs", "wetware-rs"):
                    cand = base / repo / "target" / profile / "wetware"
                    if cand in seen:
                        continue
                    seen.add(cand)
                    yield cand

    def path(self):
        """Resolved wetware binary path, or None when unavailable."""
        for cand in self._candidates():
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        return None

    def available(self):
        return self.path() is not None

    # -- subprocess helpers ------------------------------------------------------
    def _run_cli(self, args, timeout):
        binary = self.path()
        if binary is None:
            raise RuntimeError("wetware binary not found; build rsi-wetware-rs or set WETWARE_BIN")
        try:
            proc = subprocess.run(
                [binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"wetware timed out after {timeout:.0f}s") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-300:]
            raise RuntimeError(f"wetware exited {proc.returncode}: {tail}")
        return proc.stdout

    @staticmethod
    def _parse_json(stdout):
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"wetware returned unparsable output: {stdout[:200]!r}") from exc

    # -- sync API ----------------------------------------------------------------
    def info(self, sample=False):
        args = ["--sample"] if sample else []
        args.append("info")
        return lua_sandbox.DictProxy(self._parse_json(self._run_cli(args, QUICK_TIMEOUT)))

    def run_task(self, task="digits", sample=False, wait=False, on_done=None):
        """Run the wetware CLI's plain ``run <task>`` harness (no RSI loop).

        Default (async) mode requires ``on_done``: it spawns a daemon
        thread, runs the CLI, and delivers the one-line summary through
        ``on_done`` invoked under ``lua_lock``; it returns a status table
        ``{ok=true, async=true, kind="run"}`` and raises ValueError when
        ``on_done`` is missing or RuntimeError when a run is already in
        flight. With ``wait=True`` it blocks instead and returns the
        parsed report as a DictProxy.
        """
        task = str(task).lower()
        if task not in TASKS:
            raise ValueError(f"unknown task {task!r}; try {TASKS}")
        args = ["--sample"] if sample else []
        args += ["run", task]
        if wait:
            report = self._parse_json(self._run_cli(args, self._timeout))
            self._record(True, self._summarize("run", report), report)
            return lua_sandbox.DictProxy(report)
        self._spawn("run", args, on_done)
        return self._started("run")

    # optimize/adapt are the compatibility surface for prompts written against
    # the older RSI loop: the current wetware CLI has no autonomy levels and no
    # separate optimize subcommand, so optimize() forwards to run_task() and
    # adapt() keeps its own CLI subcommand.
    def optimize(self, task="digits", sample=False, wait=False, on_done=None):
        """Compatibility alias for run_task (same return contract)."""
        return self.run_task(task, sample=sample, wait=wait, on_done=on_done)

    def adapt(self, on_done=None, seed=None, sample=False, wait=False):
        """Run the L4 online-adaptation demo under distribution drift.

        Same contract as run_task: async mode requires ``on_done`` and
        returns a status table; ``wait=True`` blocks and returns the
        parsed report as a DictProxy.
        """
        args = ["--sample"] if sample else []
        args.append("adapt")
        if seed is not None:
            args.extend(["--seed", str(seed)])
        if wait:
            report = self._parse_json(self._run_cli(args, self._timeout))
            self._record(True, self._summarize("adapt", report), report)
            return lua_sandbox.DictProxy(report)
        self._spawn("adapt", args, on_done)
        return self._started("adapt")

    def bank(self):
        """Print the experience bank and derived policy."""
        return lua_sandbox.DictProxy(self._parse_json(self._run_cli(["bank"], QUICK_TIMEOUT)))

    # -- async delivery -----------------------------------------------------------
    @staticmethod
    def _started(kind):
        return lua_sandbox.DictProxy({"ok": True, "async": True, "kind": kind})

    def _spawn(self, kind, args, on_done):
        if on_done is None:
            raise ValueError("async runs require an on_done callback")
        with self._lock:
            if self._running:
                raise RuntimeError(f"fly brain is already running a {kind}")
            self._running = True

        def worker():
            ok, summary, report = True, "", None
            try:
                report = self._parse_json(self._run_cli(args, self._timeout))
                summary = self._summarize(kind, report)
            except Exception as exc:  # noqa: BLE001
                ok, summary = False, f"fly brain: {kind} failed: {exc}"
            self._record(ok, summary, report)
            with self._lua_lock:
                try:
                    on_done(summary)
                except Exception as exc:  # noqa: BLE001
                    log.warning("flybrain on_done failed: %s", exc)

        threading.Thread(target=worker, daemon=True).start()

    def _record(self, ok, summary, report):
        with self._lock:
            self._running = False
            self._last = (ok, summary, report)

    def running(self):
        with self._lock:
            return self._running

    def last(self):
        """Last finished run as {ok, text, report} (DictProxy), or None."""
        with self._lock:
            if self._last is None:
                return None
            ok, summary, report = self._last
        out = {"ok": ok, "text": summary}
        if report is not None:
            out["report"] = lua_sandbox.DictProxy(report)
        return lua_sandbox.DictProxy(out)

    # -- human-readable summaries ---------------------------------------------------
    def status(self):
        lines = []
        binary = self.path()
        lines.append(f"fly brain bridge: {'ready' if binary else 'no binary (set WETWARE_BIN)'}")
        if binary:
            lines.append(f"binary: {binary}")
        lines.append(f"running: {'yes' if self.running() else 'no'}")
        with self._lock:
            last = self._last
        if last is not None:
            lines.append(f"last run: {last[1]}")
        return "\n".join(lines)

    @staticmethod
    def _summarize(kind, report):
        if kind == "run":
            task = report.get("task", "?")
            parts = [f"fly brain: {task} done"]
            test = report.get("test")
            default = report.get("default_test")
            improvement = report.get("improvement")
            if isinstance(test, (int, float)):
                score = f"test {test:.4f}"
                if isinstance(default, (int, float)):
                    score += f" (default {default:.4f})"
                if isinstance(improvement, (int, float)):
                    score += f", improvement {improvement:+.4f}"
                parts.append(score)
            entries = report.get("bank_entries")
            if isinstance(entries, int):
                parts.append(f"bank {entries} entries")
            return "; ".join(parts)
        if kind == "adapt":
            parts = ["fly brain: l4 drift rehearsal done"]
            gated = report.get("l4_gated_val")
            naive = report.get("l4_naive_val")
            advantage = report.get("gate_advantage")
            if isinstance(gated, (int, float)) and isinstance(naive, (int, float)):
                parts.append(f"gated {gated:.4f} vs naive {naive:.4f}")
                if isinstance(advantage, (int, float)):
                    parts.append(f"({advantage:+.4f})")
            for key, label in (("adaptations", "adaptations"), ("rollbacks", "rollbacks")):
                value = report.get(key)
                if isinstance(value, int):
                    parts.append(f"{value} {label}")
            return " ".join(parts)
        return f"fly brain: {kind} finished"
