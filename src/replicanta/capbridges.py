"""Generic capability bridges exposed to Lua modules as ctx.process/http/fs/json.

The Lua sandbox blocks os/io/require/load (see lua_sandbox.py); these
bridges are the sanctioned replacements, so module Lua can touch the
world without a bespoke Python service per integration:

- ``ctx.process``: managed child processes — plain pipes, or the pty
  recipe terminal games need (tty on fd 0, keys read from fd 2, frames
  on stdout). Stream callbacks fire on the reader thread under the host
  lua_lock, the pattern fly_brain established for ``deliver``.
- ``ctx.http``: capped http(s) GET/POST.
- ``ctx.fs``: filesystem access scoped to the organism directory.
- ``ctx.json``: parse/encode plain data.

Hook scripts (``build_hook_ctx``) deliberately receive none of these —
bridges are a module privilege. Everything here is fail-soft: errors
come back as ``nil, "message"`` so Lua can pcall or route them to ctx.log.
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


class _Child:
    """One spawned process: reader thread -> on_data, watcher -> on_exit."""

    def __init__(self, argv, pty, on_data, on_exit, lua_lock, on_gone, emit):
        self.argv = argv
        self.pty = pty
        self._on_data = on_data
        self._on_exit = on_exit
        self._lua_lock = lua_lock
        self._on_gone = on_gone
        self._emit = emit
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
        threading.Thread(target=self._read_loop, daemon=True, name="capbridge-reader").start()
        threading.Thread(target=self._watch_loop, daemon=True, name="capbridge-watcher").start()
        with _ACTIVE_LOCK:
            _ACTIVE.add(self)

    def running(self) -> bool:
        return self._proc is not None and not self._stopped.is_set() and self._proc.poll() is None

    def kill(self) -> None:
        self._stopped.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=KILL_GRACE)
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=KILL_GRACE)
        if self._master is not None:
            with contextlib.suppress(OSError):
                os.close(self._master)
            self._master = None
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
            if self._lua_lock is not None:
                with self._lua_lock:
                    self._on_data(text)
            else:
                self._on_data(text)
        except Exception as exc:  # noqa: BLE001 — a bad callback must not kill the reader
            self._emit(f"process callback failed: {exc}")

    def _watch_loop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.wait()
        self._stopped.set()
        self._on_gone(self)
        if self._on_exit is not None:
            try:
                if self._lua_lock is not None:
                    with self._lua_lock:
                        self._on_exit(code)
                else:
                    self._on_exit(code)
            except Exception as exc:  # noqa: BLE001
                self._emit(f"process exit callback failed: {exc}")


class ProcessBridge:
    """ctx.process: bounded child-process management for one module."""

    def __init__(self, lua_lock=None, emit=None):
        self._lua_lock = lua_lock
        self._emit = emit or (lambda msg: None)
        self._lock = threading.Lock()
        self._procs: dict[int, _Child] = {}
        self._next = 0

    def spawn(self, argv, opts=None):
        try:
            args = [str(a) for a in argv]
        except Exception as exc:  # noqa: BLE001
            return None, f"argv: {exc}"
        if not args:
            return None, "empty argv"
        if "/" not in args[0]:
            found = shutil.which(args[0])
            if found is None:
                return None, f"binary not found: {args[0]}"
            args[0] = found
        with self._lock:
            if len(self._procs) >= MAX_PROCS_PER_MODULE:
                return None, "process limit reached (2 per module)"
        pty = bool(opts and opts.get("pty"))
        on_data = opts.get("on_data") if opts else None
        on_exit = opts.get("on_exit") if opts else None
        child = _Child(args, pty, on_data, on_exit, self._lua_lock, self._drop, self._emit)
        try:
            child.start()
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
        with self._lock:
            self._next += 1
            pid = self._next
            self._procs[pid] = child
        return pid, None

    def write(self, pid, data) -> None:
        child = self._child(pid)
        child.write(str(data))

    def kill(self, pid) -> None:
        child = self._child(pid)
        child.kill()
        self._drop(child)

    def running(self, pid) -> bool:
        return self._child(pid).running()

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
        url = str(url)
        if not url.startswith(("http://", "https://")):
            return None, "only http(s) URLs are allowed"
        timeout = float(opts.get("timeout", HTTP_TIMEOUT)) if opts else HTTP_TIMEOUT
        headers = {}
        if opts:
            raw_headers = opts.get("headers")
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
        except Exception as exc:  # noqa: BLE001 — network errors are Lua-catchable
            return None, str(exc)
        if len(raw) > HTTP_MAX_BYTES:
            return None, "response too large"
        return {"status": status, "body": raw.decode("utf-8", "replace")}, None


class FsBridge:
    """ctx.fs: read/write/list scoped to the organism directory."""

    def __init__(self, root):
        self._root = Path(root).resolve() if root else None

    def read(self, rel):
        path, err = self._resolve(rel)
        if err:
            return None, err
        if not path.is_file():
            return None, "not a file"
        if path.stat().st_size > FS_MAX_READ:
            return None, "file too large"
        return path.read_text(), None

    def write(self, rel, content):
        path, err = self._resolve(rel)
        if err:
            return None, err
        text = str(content)
        if len(text.encode()) > FS_MAX_WRITE:
            return None, "content too large"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return True, None

    def list(self, rel):
        path, err = self._resolve(rel)
        if err:
            return None, err
        if not path.is_dir():
            return None, "not a directory"
        return sorted(p.name for p in path.iterdir()), None

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
            return json.loads(str(text)), None
        except Exception as exc:  # noqa: BLE001
            return None, f"invalid json: {exc}"

    def encode(self, value):
        try:
            return json.dumps(_plain(value), ensure_ascii=False), None
        except Exception as exc:  # noqa: BLE001
            return None, f"cannot encode: {exc}"


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


class Bridges:
    """The four bridges bound to one module context."""

    def __init__(self, lua_lock=None, organism_dir=None, emit=None):
        self.process = ProcessBridge(lua_lock=lua_lock, emit=emit)
        self.http = HttpBridge()
        self.fs = FsBridge(organism_dir)
        self.json = JsonBridge()


def build(lua_lock=None, organism_dir=None, emit=None) -> Bridges:
    return Bridges(lua_lock=lua_lock, organism_dir=organism_dir, emit=emit)


def shutdown_all() -> None:
    """Kill every child still tracked (module reload / app exit)."""
    with _ACTIVE_LOCK:
        children = list(_ACTIVE)
    for child in children:
        with contextlib.suppress(Exception):
            child.kill()
