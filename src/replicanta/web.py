"""Local Glasshouse web front-end for Replicanta.

The server deliberately uses only the Python standard library.  It binds to
loopback by default, serves a small bundled client, and delegates every state
change to the same public organism/nursery APIs used by the TUI.
"""

import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from replicanta import extensions, nursery, voice
from replicanta.organism import Organism
from replicanta.web_static import APP_CSS, APP_HTML, APP_JS


class WebError(ValueError):
    """A safe client-facing request error."""


class Glasshouse:
    """Thread-safe adapter between HTTP requests and one live organism."""

    def __init__(self, root, organism, spawn=None, respond=voice.respond):
        self.root = Path(root)
        self.org = organism
        self.spawn = dict(spawn or {})
        self.respond = respond
        self.lock = threading.RLock()

    @property
    def name(self):
        return self.org.dir_path.name

    @property
    def extension_path(self):
        return self.org.dir_path / "artifacts" / "extensions.json"

    def snapshot(self):
        with self.lock:
            store = self.org.store
            metrics = self.org.metrics()
            beliefs = [
                {
                    "subject": o,
                    "relation": a,
                    "value": v,
                    "confidence": round(c, 3),
                }
                for (o, a, v), c in sorted(
                    store.beliefs().items(), key=lambda item: -item[1]
                )
            ]
            skills = [
                {
                    "name": skill.name,
                    "when": skill.when,
                    "uses": skill.uses,
                    "effectiveness": skill.effectiveness,
                }
                for skill in self.org.skills.list()
            ]
            registry = extensions.registry()
            return {
                "organism": {
                    "name": self.name,
                    "state": self.org.lifecycle.state,
                    "mood": store.belief_value("self", "mood", "calm"),
                    "cycle": store.cycle,
                    "stress": round(store.stress, 3),
                    "arousal": round(store.arousal, 3),
                    "rationality": round(store.rationality, 3),
                    "irrationality": round(store.irrationality, 3),
                    "chaos": round(store.chaos, 3),
                    "auto_apply": bool(store.auto_apply_patches),
                },
                "metrics": {
                    "beliefs": metrics.belief_count,
                    "rules": metrics.rule_count,
                    "depth": metrics.total_depth,
                    "activity": dict(store.activity),
                },
                "beliefs": beliefs,
                "memory": list(reversed(store.memory[-50:])),
                "chat": [{"role": role, "text": text} for role, text in store.chat_log],
                "goals": list(store.goals),
                "skills": skills,
                "attention": [list(pair) for pair in sorted(store.attention)],
                "extensions": {
                    "version": registry.get("version", 0),
                    "pending": registry.get("pending"),
                    "applied": registry.get("entries", []),
                },
                "nursery": {
                    "current": self.name,
                    "organisms": nursery.list_organisms(self.root),
                    "groups": nursery.load_groups(self.root),
                },
            }

    def chat(self, text):
        text = str(text).strip()
        if not text or len(text) > 4000:
            raise WebError("message must contain 1-4000 characters")
        with self.lock:
            events = self.org.hear(text)
            reply = self.respond(self.org, text)
            if not reply:
                reply = "I am quiet for now."
            self.org.store.record_chat("org", reply)
            self.org.flush(force=True)
            return {"reply": reply, "events": events, "state": self.snapshot()}

    def lifecycle(self, action):
        with self.lock:
            if action in ("wake", "sleep"):
                events = self.org.force_state(action)
            elif action == "revive":
                events = [{"kind": "revive", "changed": self.org.revive()}]
            else:
                raise WebError("lifecycle action must be wake, sleep, or revive")
            self.org.flush(force=True)
            return {"events": events, "state": self.snapshot()}

    def settings(self, data):
        with self.lock:
            if "chaos" in data:
                chaos = float(data["chaos"])
                if not 0 <= chaos <= 1:
                    raise WebError("chaos must be between 0 and 1")
                self.org.store.chaos = chaos
                self.org.store.dirty = True
            if "auto_apply" in data:
                self.org.store.auto_apply_patches = bool(data["auto_apply"])
                self.org.store.dirty = True
            if "focus" in data:
                focus = str(data["focus"]).strip()
                if focus:
                    self.org.store.attention = {
                        (a, v)
                        for _o, a, v in self.org.store.beliefs()
                        if focus.lower() in a.lower() or focus.lower() in v.lower()
                    }
                else:
                    self.org.store.attention = set()
                self.org.store.dirty = True
            self.org.flush(force=True)
            return self.snapshot()

    def mutation(self, action):
        with self.lock:
            operations = {
                "approve": extensions.approve,
                "reject": extensions.reject,
                "revert": extensions.revert_last,
            }
            if action not in operations:
                raise WebError("mutation action must be approve, reject, or revert")
            entry = operations[action](self.extension_path)
            if entry:
                self.org.store.remember("skill", f"patch {action}d ({entry['kind']})")
                self.org.flush(force=True)
            return {"entry": entry, "state": self.snapshot()}

    def create_organism(self, name):
        with self.lock:
            name = str(name).strip()
            nursery.create(self.root, name, self.root / "organism.scl")
            return self._swap(name)

    def swap(self, name):
        with self.lock:
            if name not in nursery.list_organisms(self.root):
                raise WebError(f"no organism named {name!r}")
            return self._swap(name)

    def _swap(self, name):
        self.org.flush(force=True)
        nursery.set_current(self.root, name)
        org = Organism(nursery.organism_dir(self.root, name), **self.spawn)
        org.load()
        self.org = org
        return self.snapshot()


class GlasshouseHandler(BaseHTTPRequestHandler):
    server_version = "ReplicantaGlasshouse/0.1"

    @property
    def app(self):
        return self.server.glasshouse

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            return self._json(HTTPStatus.OK, self.app.snapshot())
        assets = {
            "/": ("text/html; charset=utf-8", APP_HTML),
            "/app.css": ("text/css; charset=utf-8", APP_CSS),
            "/app.js": ("text/javascript; charset=utf-8", APP_JS),
        }
        if path in assets:
            kind, body = assets[path]
            return self._send(HTTPStatus.OK, kind, body.encode())
        return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._body()
            routes = {
                "/api/chat": lambda: self.app.chat(data.get("text", "")),
                "/api/lifecycle": lambda: self.app.lifecycle(data.get("action")),
                "/api/settings": lambda: self.app.settings(data),
                "/api/mutation": lambda: self.app.mutation(data.get("action")),
                "/api/organisms": lambda: self.app.create_organism(data.get("name", "")),
                "/api/swap": lambda: self.app.swap(data.get("name", "")),
            }
            if path not in routes:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return self._json(HTTPStatus.OK, routes[path]())
        except (WebError, ValueError, TypeError) as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (OSError, RuntimeError):  # never expose local paths or prompts
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "request failed"})

    def _body(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise WebError("invalid content length") from exc
        if size > 1_000_000:
            raise WebError("request too large")
        try:
            return json.loads(self.rfile.read(size) or b"{}")
        except json.JSONDecodeError as exc:
            raise WebError("invalid JSON") from exc

    def _json(self, status, value):
        return self._send(status, "application/json", json.dumps(value).encode())

    def _send(self, status, kind, body):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


def make_server(glasshouse, host="127.0.0.1", port=8765):
    # Scallop contexts are deliberately thread-affine. A single-threaded
    # server keeps every reasoner operation on the thread that created the
    # organism; long LLM replies simply serialize local requests.
    server = HTTPServer((host, port), GlasshouseHandler)
    server.glasshouse = glasshouse
    return server


def run(root, organism, spawn=None, host="127.0.0.1", port=8765, open_browser=True):
    server = make_server(Glasshouse(root, organism, spawn=spawn), host, port)
    url = f"http://{host}:{server.server_port}"
    print(f"Replicanta Glasshouse: {url}")
    if open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.glasshouse.org.flush(force=True)
        server.server_close()
