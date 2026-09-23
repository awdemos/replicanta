"""Generic capability bridges exposed to Lua modules as ctx.process/http/fs/json.

The Lua sandbox blocks os/io/require/load (see lua_sandbox.py); these
bridges are the sanctioned replacements, so module Lua can touch the
world without a bespoke Python service per integration:

- ``ctx.process``: managed child processes — plain pipes, or the pty
  recipe terminal games need (tty on fd 0, keys read from fd 2, frames
  on stdout). Stream callbacks fire on the reader thread directly; lupa
  serializes runtime access, and deliberately NOT taking the host lua_lock
  avoids an ABBA inversion with api calls that emit events (runtime -> host)
  from the UI thread.
- ``ctx.http``: capped http(s) GET/POST.
- ``ctx.fs``: filesystem access scoped to the organism directory.
- ``ctx.json``: parse/encode plain data.
- ``ctx.after`` / ``ctx.every``: timer threads that invoke a Lua function
  with pcall containment (handlers run OFF the UI thread), and ``ctx.kv``:
  file-backed per-module JSON storage scoped under the organism directory.

Hook scripts (``build_hook_ctx``) deliberately receive none of these —
bridges are a module privilege. Everything here is fail-soft: errors
come back as ``nil, "message"`` so Lua can pcall or route them to ctx.log.

Child processes carry an ``owner`` tag (the module loader's registry) so
``shutdown_all(owner=...)`` only kills children spawned by that registry's
modules — one organism's module reload must not reap another's games.
``shutdown_all()`` with no owner kills everything (app exit / atexit).
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

MAX_PROCS_PER_MODULE = 2
OUTPUT_CAP = 256 * 1024  # per-child retained output (newest kept)
STDERR_CAP = 8 * 1024
KILL_GRACE = 1.5
HTTP_TIMEOUT = 10.0
HTTP_MAX_BYTES = 1024 * 1024
FS_MAX_READ = 1024 * 1024
FS_MAX_WRITE = 5 * 1024 * 1024

_ACTIVE: set[_Child] = set()
_ACTIVE_LOCK = threading.Lock()


def _reap_all() -> None:
    for child in list(_ACTIVE):
        with contextlib.suppress(Exception):
            child.kill()


atexit.register(_reap_all)


def _opt(opts, key, default=None):
    """Read an option from a Python dict or a lupa Lua table (nil-safe)."""
    if opts is None:
        return default
    try:
        value = opts[key]
    except Exception:  # noqa: BLE001 — non-indexable fallback
        getter = getattr(opts, "get", None)
        value = getter(key, default) if callable(getter) else default
    return default if value is None else value


class _Child:
    """One spawned process: reader thread -> on_data, watcher -> on_exit."""

    def __init__(self, argv, pty, on_data, on_exit, on_gone, emit, owner=None):
        self.argv = argv
        self.pty = pty
        self._on_data = on_data
        self._on_exit = on_exit
        self._on_gone = on_gone
        self._emit = emit
        self.owner = owner  # registry tag: scopes shutdown_all(owner=...)
        self._proc: subprocess.Popen | None = None
        self._master: int | None = None
        self._buf = bytearray()
        self._err = bytearray()
        self._stopped = threading.Event()
        self.pid: int | None = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        master = None
        slave = None
        if self.pty:
            master, slave = os.openpty()
        try:
            self._proc = subprocess.Popen(
                self.argv,
                stdin=slave if self.pty else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=slave if self.pty else subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            if slave is not None:
                os.close(slave)
        self._master = master
        self.pid = self._proc.pid
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="capbridge-reader")
        self._reader.start()
        threading.Thread(target=self._watch_loop, daemon=True, name="capbridge-watcher").start()
        with _ACTIVE_LOCK:
            _ACTIVE.add(self)

    def running(self) -> bool:
        return self._proc is not None and not self._stopped.is_set() and self._proc.poll() is None

    def kill(self) -> None:
        """Ask the child to die and return IMMEDIATELY. Callers run on the UI
        thread, so this must never block: SIGTERM goes out now and the
        watcher thread reaps the child, escalating to SIGKILL when it
        survives the grace window. Reaping may lag this call by up to a few
        grace periods — callers that need the child gone must poll
        running()/wait() themselves."""
        self._stopped.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
        self._close_master()
        with _ACTIVE_LOCK:
            _ACTIVE.discard(self)

    def _close_master(self) -> None:
        """Close the pty master exactly once (kill() and the watcher race)."""
        master, self._master = self._master, None
        if master is not None:
            with contextlib.suppress(OSError):
                os.close(master)

    def _release_active(self) -> None:
        """Exit bookkeeping: leave the atexit-reap set once the child has
        finished. The bridge keeps the child tracked (for output()/kill())
        until the owner reaps it with kill()."""
        with _ACTIVE_LOCK:
            _ACTIVE.discard(self)

    # -- io --------------------------------------------------------------------
    def write(self, data: str) -> None:
        payload = str(data).encode()
        if self._master is not None:
            os.write(self._master, payload)
            return
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("process is not running")
        proc.stdin.write(payload)
        proc.stdin.flush()

    def output(self) -> bytes:
        return bytes(self._buf)

    def stderr_text(self) -> str:
        return bytes(self._err).decode("utf-8", "replace")

    # -- threads -----------------------------------------------------------------
    def _read_loop(self) -> None:
        proc = self._proc
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        if self._master is not None:
            selector.register(self._master, selectors.EVENT_READ)
        while not self._stopped.is_set():
            try:
                events = selector.select(timeout=0.1)
            except (OSError, ValueError):
                break
            for key, _ in events:
                try:
                    chunk = os.read(key.fd, 65536)
                except OSError:
                    chunk = b""
                if key.fileobj is proc.stdout:
                    if not chunk:
                        return
                    self._feed(chunk)
                elif chunk:
                    # fd-2 writes (game keystroke reads don't echo, so this
                    # is stderr diagnostics) kept for failure reports.
                    self._err = (self._err + chunk)[-STDERR_CAP:]

    def _feed(self, chunk: bytes) -> None:
        self._buf = (self._buf + chunk)[-OUTPUT_CAP:]
        if self._on_data is None:
            return
        text = chunk.decode("utf-8", "replace")
        try:
            # No host lua_lock here: lupa serializes runtime access, and
            # taking the host lock from a reader thread invites ABBA
            # deadlock against UI-thread api calls that emit events.
            self._on_data(text)
        except Exception as exc:  # noqa: BLE001 — a bad callback must not kill the reader
            self._emit(f"process callback failed: {exc}")

    def _watch_loop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        # Bounded waits, not one unbounded proc.wait(): a child that ignores
        # SIGTERM (kill() already signaled) must be escalated to SIGKILL by
        # THIS thread, since kill() no longer waits. Long-running children
        # that were not asked to stop just keep timing out and re-waiting.
        escalated = False
        while True:
            try:
                code = proc.wait(timeout=KILL_GRACE)
                break
            except subprocess.TimeoutExpired:
                if self._stopped.is_set() and not escalated:
                    escalated = True
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(proc.pid, signal.SIGKILL)
        # Take over from the reader thread so on_exit callbacks observe the
        # COMPLETE stream: stop the reader, join it, then drain to EOF.
        self._stopped.set()
        reader = self._reader
        if reader is not None:
            reader.join(timeout=0.5)
        if proc.stdout is not None:
            with contextlib.suppress(OSError):
                while True:
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    self._feed(chunk)
        self._close_master()
        if self._on_exit is not None:
            try:
                self._on_exit(code)
            except Exception as exc:  # noqa: BLE001
                self._emit(f"process exit callback failed: {exc}")
        # Still tracked by the owning bridge for output()/kill(); only the
        # atexit-reap set is released.
        self._release_active()


class ProcessBridge:
    """ctx.process: bounded child-process management for one module."""

    def __init__(self, emit=None, owner=None):
        self._emit = emit or (lambda msg: None)
        self._owner = owner  # registry tag: scopes capbridges.shutdown_all
        self._lock = threading.Lock()
        self._procs: dict[int, _Child] = {}
        self._next = 0

    def spawn(self, argv, opts=None):
        """Spawn a child; returns the process id. Raises ValueError with a
        Lua-readable message on failure (project convention: bridges raise,
        modules pcall)."""
        from lupa import lua_type

        try:
            if lua_type(argv) == "table":
                args = [str(argv[i]) for i in range(1, len(argv) + 1)]
            else:
                args = [str(a) for a in argv]
        except Exception as exc:
            raise ValueError(f"argv: {exc}") from exc
        if not args:
            raise ValueError("empty argv")
        if "/" not in args[0]:
            found = shutil.which(args[0])
            if found is None:
                raise ValueError(f"binary not found: {args[0]}")
            args[0] = found
        with self._lock:
            if len(self._procs) >= MAX_PROCS_PER_MODULE:
                raise ValueError("process limit reached (2 per module)")
        pty = bool(_opt(opts, "pty"))
        on_data = _opt(opts, "on_data")
        on_exit = _opt(opts, "on_exit")
        child = _Child(args, pty, on_data, on_exit, self._drop, self._emit, owner=self._owner)
        # Register before the threads start: a fast-dying child's on_exit
        # callback must already see the process through output()/kill().
        with self._lock:
            self._next += 1
            pid = self._next
            self._procs[pid] = child
        try:
            child.start()
        except Exception as exc:
            self._drop(child)
            raise ValueError(str(exc)) from exc
        return pid

    def write(self, pid, data, opts=None) -> None:
        """Write to a child's stdin. opts may carry taps/spacing (numbers):
        the payload is written that many times, spacing seconds apart, from
        a bridge thread. doom-ascii's input is a 42ms-held timestamp per
        read, so a single tap moves the player barely a step; spaced taps
        keep the key refreshed for a visible, human-scale movement (and
        never block the caller — the UI thread invokes this)."""
        child = self._child(pid)
        payload = str(data)
        taps = int(_opt(opts, "taps", 1) or 1)
        spacing = float(_opt(opts, "spacing", 0.05) or 0.05)
        if taps <= 1:
            child.write(payload)
            return

        def tapper():
            for _ in range(taps):
                try:
                    if not child.running():
                        return
                    child.write(payload)
                except Exception as exc:  # noqa: BLE001 — a dead child mid-tap is normal
                    self._emit(f"process tap failed: {exc}")
                    return
                if spacing > 0:
                    time.sleep(spacing)

        threading.Thread(target=tapper, daemon=True, name="capbridge-tapper").start()

    def kill(self, pid) -> None:
        child = self._child(pid)
        child.kill()
        self._drop(child)

    def running(self, pid) -> bool:
        return self._child(pid).running()

    def wait(self, pid, timeout=None) -> int:
        """Block for the child's exit code. Raises ValueError on timeout
        (Lua-catchable); the child stays tracked so output() still works."""
        child = self._child(pid)
        try:
            if timeout is None:
                return child._proc.wait()
            return child._proc.wait(timeout=float(timeout))
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"process did not exit within {timeout:.0f}s") from exc

    def output(self, pid) -> str:
        return self._child(pid).output().decode("utf-8", "replace")

    def stderr(self, pid) -> str:
        return self._child(pid).stderr_text()

    def shutdown(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
            self._procs.clear()
        for child in procs:
            child.kill()

    def _child(self, pid) -> _Child:
        with self._lock:
            child = self._procs.get(int(pid))
        if child is None:
            raise RuntimeError(f"no such process: {pid}")
        return child

    def _drop(self, child: _Child) -> None:
        with self._lock:
            for pid, mine in list(self._procs.items()):
                if mine is child:
                    del self._procs[pid]
        with _ACTIVE_LOCK:
            _ACTIVE.discard(child)


class HttpBridge:
    """ctx.http: capped http(s) GET/POST. Blocking by design at this scale."""

    def get(self, url, opts=None):
        return self._request("GET", url, None, opts)

    def post(self, url, body=None, opts=None):
        return self._request("POST", url, body, opts)

    def _request(self, method, url, body, opts):
        """Returns {status=, body=}. Raises ValueError (Lua-catchable) on
        transport failure, non-2xx status, oversize bodies, or bad schemes."""
        url = str(url)
        if not url.startswith(("http://", "https://")):
            raise ValueError("only http(s) URLs are allowed")
        timeout = float(_opt(opts, "timeout", HTTP_TIMEOUT))
        headers = {}
        raw_headers = _opt(opts, "headers")
        if raw_headers is not None:
            headers = {str(k): str(v) for k, v in raw_headers.items()}
        data = None
        if body is not None:
            data = str(body).encode()
            headers.setdefault("Content-Type", "application/json")
        request = Request(url, data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(HTTP_MAX_BYTES + 1)
                status = response.status
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        if len(raw) > HTTP_MAX_BYTES:
            raise ValueError("response too large")
        if status >= 400:
            raise ValueError(f"http {status}")
        return {"status": status, "body": raw.decode("utf-8", "replace")}


class FsBridge:
    """ctx.fs: read/write/list scoped to the organism directory."""

    def __init__(self, root):
        self._root = Path(root).resolve() if root else None

    def read(self, rel):
        """Returns the file text; raises ValueError (Lua-catchable)."""
        path, err = self._resolve(rel)
        if err:
            raise ValueError(err)
        if not path.is_file():
            raise ValueError("not a file")
        if path.stat().st_size > FS_MAX_READ:
            raise ValueError("file too large")
        return path.read_text()

    def write(self, rel, content):
        path, err = self._resolve(rel)
        if err:
            raise ValueError(err)
        text = str(content)
        if len(text.encode()) > FS_MAX_WRITE:
            raise ValueError("content too large")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return True

    def list(self, rel):
        path, err = self._resolve(rel)
        if err:
            raise ValueError(err)
        if not path.is_dir():
            raise ValueError("not a directory")
        return sorted(p.name for p in path.iterdir())

    def _resolve(self, rel):
        if self._root is None:
            return None, "no organism directory"
        path = (self._root / str(rel)).resolve()
        if path != self._root and self._root not in path.parents:
            return None, "path escapes the organism directory"
        return path, None


class JsonBridge:
    """ctx.json: plain-data parse/encode (Lua tables convert recursively)."""

    def parse(self, text):
        try:
            from replicanta.lua_sandbox import DictProxy

            return DictProxy(json.loads(str(text)))
        except Exception as exc:
            raise ValueError(f"invalid json: {exc}") from exc

    def encode(self, value):
        try:
            return json.dumps(_plain(value), ensure_ascii=False)
        except Exception as exc:
            raise ValueError(f"cannot encode: {exc}") from exc


def _plain(value):
    """Recursively convert lupa tables to plain Python for json.dumps."""
    from lupa import lua_type

    if lua_type(value) == "table":
        keys = list(value.keys())
        if all(isinstance(k, str) for k in keys):
            return {k: _plain(value[k]) for k in keys}
        return [_plain(v) for v in value.values()]
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


class TimerHandle:
    """Cancel handle for a ctx timer. ``cancel()`` (dot or colon call) stops
    future firings; an already-queued firing may still run once."""

    def __init__(self):
        self._cancelled = False
        self._timer: threading.Timer | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self, *_ignored) -> None:
        """Stop future firings. Extra args are ignored so Lua can call this
        with either handle.cancel() or handle:cancel()."""
        self._cancelled = True
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()


class TimerBridge:
    """ctx.after / ctx.every: timer threads for Lua modules.

    Handlers run OFF the UI thread on daemon timer threads; the ``invoke``
    callable (built by the module loader) enters the module's own Lua
    runtime with pcall containment, so a raising handler becomes one
    emitted error line instead of killing the timer thread or the app.
    """

    def __init__(self, invoke, emit=None):
        self._invoke = invoke
        self._emit = emit or (lambda msg: None)

    def after(self, ms, fn) -> TimerHandle:
        """Invoke fn once after ms milliseconds."""
        handle = TimerHandle()
        delay = max(0.0, float(ms)) / 1000.0

        def run():
            if handle.cancelled:
                return
            self._invoke(fn)

        timer = threading.Timer(delay, run)
        timer.daemon = True
        handle._timer = timer
        timer.start()
        return handle

    def every(self, ms, fn) -> TimerHandle:
        """Invoke fn every ms milliseconds until the handle is cancelled."""
        handle = TimerHandle()
        interval = max(0.0, float(ms)) / 1000.0

        def run():
            if handle.cancelled:
                return
            self._invoke(fn)
            if handle.cancelled:
                return
            timer = threading.Timer(interval, run)
            timer.daemon = True
            handle._timer = timer
            timer.start()

        timer = threading.Timer(interval, run)
        timer.daemon = True
        handle._timer = timer
        timer.start()
        return handle


class KvBridge:
    """ctx.kv: file-backed per-module JSON key-value storage.

    Scoped to ``<organism_dir>/kv/<module>.json``: survives module reloads
    (the file, not the module state, is the source of truth), is
    name-spaced by module name, and never exposes a path to Lua — only the
    get/set/delete/keys verbs. Without an organism directory it degrades to
    an in-memory store. Values must be plain JSON data (lupa tables convert
    recursively); dict results come back wrapped so Lua reads keys
    naturally (see lua_sandbox.DictProxy).
    """

    def __init__(self, path, emit=None):
        self._path = Path(path) if path else None
        self._emit = emit or (lambda msg: None)
        self._lock = threading.Lock()
        self._data: dict | None = None

    def _load(self) -> None:
        if self._data is not None:
            return
        data = None
        if self._path is not None and self._path.is_file():
            try:
                data = json.loads(self._path.read_text())
            except (OSError, ValueError) as exc:
                self._emit(f"kv: unreadable store, starting empty ({exc})")
        self._data = data if isinstance(data, dict) else {}

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            from replicanta.fileutil import atomic_write_text

            atomic_write_text(self._path, json.dumps(self._data, ensure_ascii=False))
        except OSError as exc:
            raise ValueError(f"kv: cannot persist: {exc}") from exc

    def get(self, key, default=None):
        with self._lock:
            self._load()
            return _wrap_kv(self._data.get(str(key), default))

    def set(self, key, value) -> bool:
        from replicanta.lua_sandbox import to_py

        with self._lock:
            self._load()
            self._data[str(key)] = to_py(value)
            self._save()
        return True

    def delete(self, key) -> bool:
        with self._lock:
            self._load()
            existed = str(key) in self._data
            self._data.pop(str(key), None)
            if existed:
                self._save()
        return existed

    def keys(self):
        with self._lock:
            self._load()
            return sorted(self._data)


def _wrap_kv(value):
    """Wrap dict values for Lua-friendly key access (lists stay lists —
    lupa converts them to tables on the call boundary)."""
    from replicanta.lua_sandbox import DictProxy

    if isinstance(value, dict):
        return DictProxy(value)
    return value


class Bridges:
    """The bridges bound to one module context."""

    def __init__(self, organism_dir=None, emit=None, owner=None):
        self.process = ProcessBridge(emit=emit, owner=owner)
        self.http = HttpBridge()
        self.fs = FsBridge(organism_dir)
        self.json = JsonBridge()


def build(organism_dir=None, emit=None, owner=None) -> Bridges:
    return Bridges(organism_dir=organism_dir, emit=emit, owner=owner)


def shutdown_all(owner=None) -> None:
    """Kill tracked children and return immediately.

    Idempotent and safe to call twice. With ``owner`` (a registry tag),
    only children spawned by that registry's modules are killed — a
    per-organism module reload must not reap other organisms' games. With
    no owner, everything dies (module reload of last resort / app exit).
    """
    with _ACTIVE_LOCK:
        if owner is None:
            children = list(_ACTIVE)
        else:
            children = [child for child in _ACTIVE if child.owner is owner]
    for child in children:
        with contextlib.suppress(Exception):
            child.kill()
