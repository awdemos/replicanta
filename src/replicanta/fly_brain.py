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
LEVELS = ("l1", "l2", "l3", "l4", "l5")
DEFAULT_TIMEOUT = 1800.0
QUICK_TIMEOUT = 120.0


class FlyBrainService:
    """Subprocess bridge to the ``wetware`` CLI.

    Exposed to Lua as ``services.get('flybrain')`` with methods:
      path(), available(), status(), info(sample), bank(),
      optimize(task, budget, level, seed, sample, wait, on_done),
      adapt(seed, sample, wait, on_done), running(), last().

    ``optimize``/``adapt`` run the full L1-L5 improvement loop, which takes
    minutes on the real connectome, so they default to async: spawn a
    daemon thread, run the CLI, then deliver a one-line summary through
    ``on_done`` (a Lua function) invoked under ``lua_lock``. With
    ``wait=True`` they block and return the parsed report as a DictProxy.
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
        for base in bases:
            for profile in ("release", "debug"):
                cand = base / "rsi-wetware-rs" / "target" / profile / "wetware"
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

    def bank(self):
        return self._run_cli(["bank"], QUICK_TIMEOUT).strip()

    def optimize(self, task="digits", budget=None, level=None, on_done=None, seed=None, sample=False, wait=False):
        task = str(task).lower()
        if task not in TASKS:
            raise ValueError(f"unknown task {task!r}; try {TASKS}")
        level = str(level or "l5").lower()
        if level not in LEVELS:
            raise ValueError(f"unknown level {level!r}; try {LEVELS}")
        args = ["--sample"] if sample else []
        args += ["optimize", task, "--level", level, "--quiet"]
        if budget is not None:
            args += ["--budget", str(int(budget))]
        if seed is not None:
            args += ["--seed", str(int(seed))]
        if wait:
            report = self._parse_json(self._run_cli(args, self._timeout))
            self._record(True, self._summarize("optimize", report), report)
            return lua_sandbox.DictProxy(report)
        self._spawn("optimize", args, on_done)
        return True

    def adapt(self, on_done=None, seed=None, sample=False, wait=False):
        args = ["--sample"] if sample else []
        args += ["adapt", "--quiet"]
        if seed is not None:
            args += ["--seed", str(int(seed))]
        if wait:
            report = self._parse_json(self._run_cli(args, self._timeout))
            self._record(True, self._summarize("adapt", report), report)
            return lua_sandbox.DictProxy(report)
        self._spawn("adapt", args, on_done)
        return True

    # -- async delivery -----------------------------------------------------------
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
        try:
            head = self.bank().splitlines()[0]
        except Exception as exc:  # noqa: BLE001
            head = f"bank unavailable: {exc}"
        lines.append(head)
        return "\n".join(lines)

    @staticmethod
    def _summarize(kind, report):
        if kind == "optimize":
            task = report.get("task", "?")
            level = report.get("level", "?")
            parts = [f"fly brain: {task} {level} done"]
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
