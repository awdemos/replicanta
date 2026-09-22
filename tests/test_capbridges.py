"""Capability bridges: ctx.process / ctx.http / ctx.fs / ctx.json contracts.

Python-level tests; the Lua binding is exercised end-to-end by the
doom-ascii and fly-brain module suites.
"""

import http.server
import os
import threading
import time
from pathlib import Path

import pytest

from replicanta import capbridges

STUB = str(Path(__file__).parent / "fixtures" / "doom_ascii_stub.py")


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- process bridge -------------------------------------------------------------


def test_spawn_pipe_mode_streams_output_and_kill_reaps():
    got = []
    bridge = capbridges.ProcessBridge()
    pid, err = bridge.spawn([STUB, "--frames", "200", "--interval", "0.02"], {"on_data": got.append})
    assert err is None and pid is not None
    assert _wait_for(lambda: "DOOM-ASCII STUB" in "".join(got))
    child_pid = bridge._procs[pid].pid
    bridge.kill(pid)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    with pytest.raises(RuntimeError, match="no such process"):
        bridge.running(pid)


def test_spawn_pty_mode_writes_reach_the_child():
    got = []
    bridge = capbridges.ProcessBridge()
    pid, err = bridge.spawn([STUB, "--interval", "0.02"], {"pty": True, "on_data": got.append})
    assert err is None
    assert _wait_for(lambda: "DOOM-ASCII STUB" in "".join(got))
    bridge.write(pid, "\x1b[A")  # up arrow -> stub shows key=up
    assert _wait_for(lambda: "key=up" in "".join(got))
    bridge.kill(pid)


def test_spawn_limit_two_per_module():
    bridge = capbridges.ProcessBridge()
    p1, _ = bridge.spawn([STUB, "--interval", "0.05"], {})
    p2, _ = bridge.spawn([STUB, "--interval", "0.05"], {})
    assert p1 is not None and p2 is not None
    p3, err = bridge.spawn([STUB, "--interval", "0.05"], {})
    assert p3 is None and "limit" in err
    bridge.kill(p1)
    bridge.kill(p2)


def test_spawn_missing_binary_reports_error():
    bridge = capbridges.ProcessBridge()
    pid, err = bridge.spawn(["definitely-not-a-real-binary-xyz"], {})
    assert pid is None and "not found" in err


def test_pty_stderr_captured_for_failure_reports():
    got = []
    bridge = capbridges.ProcessBridge()
    pid, _ = bridge.spawn([STUB, "--die", "--interval", "0.02"], {"pty": True, "on_data": got.append})
    assert _wait_for(lambda: "IWAD or PWAD" in bridge.stderr(pid))
    bridge.kill(pid)


def test_shutdown_all_kills_tracked_children():
    bridge = capbridges.ProcessBridge()
    pid, _ = bridge.spawn([STUB, "--interval", "0.05"], {})
    child = bridge._procs[pid]
    capbridges.shutdown_all()
    assert not child.running()


# -- http bridge ------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/big":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * (capbridges.HTTP_MAX_BYTES + 10))
        else:
            body = b"hello"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"echo:" + body)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_url():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_http_get_and_post(http_url):
    http = capbridges.HttpBridge()
    res, err = http.get(http_url)
    assert err is None and res["status"] == 200 and res["body"] == "hello"
    res, err = http.post(http_url + "/echo", "data")
    assert err is None and res["body"] == "echo:data"


def test_http_blocks_non_http_and_caps_size(http_url):
    http = capbridges.HttpBridge()
    res, err = http.get("file:///etc/passwd")
    assert res is None and "http" in err
    res, err = http.get(http_url + "/big")
    assert res is None and "too large" in err


# -- fs bridge ----------------------------------------------------------------------


def test_fs_scoped_read_write_list(tmp_path):
    fs = capbridges.FsBridge(tmp_path)
    ok, err = fs.write("notes/a.txt", "hi")
    assert err is None and ok is True
    text, err = fs.read("notes/a.txt")
    assert err is None and text == "hi"
    names, err = fs.list("notes")
    assert err is None and names == ["a.txt"]


def test_fs_rejects_escape(tmp_path):
    fs = capbridges.FsBridge(tmp_path)
    res, err = fs.read("../outside.txt")
    assert res is None and "escapes" in err
    res, err = fs.write("../../somewhere/x", "nope")
    assert res is None and "escapes" in err


# -- json bridge ----------------------------------------------------------------------


def test_json_round_trip_and_errors():
    jb = capbridges.JsonBridge()
    value, err = jb.parse('{"a": [1, 2], "b": "x"}')
    assert err is None and value == {"a": [1, 2], "b": "x"}
    text, err = jb.encode({"a": [1, 2]})
    assert err is None and '"a"' in text
    res, err = jb.parse("{nope")
    assert res is None and "invalid json" in err


# -- lua wiring -------------------------------------------------------------------------


def test_module_ctx_exposes_bridges(tmp_path):
    from replicanta.modules import ModuleLoader

    mod = tmp_path / "modules" / "probemod"
    mod.mkdir(parents=True)
    (mod / "manifest.toml").write_text('name = "probemod"\nprovides = []\ndepends = []\n')
    (mod / "init.lua").write_text(
        "function init(ctx)\n"
        "  local commands = ctx.services.get('commands')\n"
        "  commands:register('/probe', function(args)\n"
        "    return string.format('bridges %s %s %s %s',\n"
        "      tostring(ctx.process ~= nil), tostring(ctx.http ~= nil),\n"
        "      tostring(ctx.fs ~= nil), tostring(ctx.json ~= nil))\n"
        "  end)\n"
        "end\n"
    )
    loader = ModuleLoader(tmp_path / "modules", organism=None, modules_config={"enabled": ["base", "probemod"]})
    loader.load_all()
    assert "probemod" in loader.modules
    out = loader.registry.get("commands").dispatch("/probe", [])
    assert out == "bridges true true true true"
