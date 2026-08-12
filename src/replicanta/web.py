"""Local Glasshouse web front-end for Replicanta.

The server deliberately uses only the Python standard library.  It binds to
loopback by default, serves a small bundled client, and delegates every state
change to the same public organism/nursery APIs used by the TUI.
"""

import json
import shutil
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

    def _swap(self, name, flush_old=True):
        if flush_old:
            self.org.flush(force=True)
        nursery.set_current(self.root, name)
        org = Organism(nursery.organism_dir(self.root, name), **self.spawn)
        org.load()
        self.org = org
        return self.snapshot()

    def rename(self, name):
        with self.lock:
            name = str(name).strip()
            if not name:
                raise WebError("rename needs a name")
            if name == self.name:
                return self.snapshot()
            self.org.flush(force=True)
            nursery.rename(self.root, self.name, name)
            return self._swap(name)

    def cycle(self):
        with self.lock:
            self.org.cycle()
            self.org.flush(force=True)
            return {"state": self.snapshot()}

    def remember(self, text):
        with self.lock:
            text = str(text).strip()
            if not text:
                raise WebError("remember needs text")
            self.org.store.remember("user", text)
            self.org.flush(force=True)
            return {"state": self.snapshot()}

    def forget(self, text):
        with self.lock:
            text = str(text).strip()
            if not text:
                raise WebError("forget needs text")
            lowered = text.lower()
            store = self.org.store
            before = (
                len(store.goals)
                + len(store.memory)
                + len(store.attention)
                + len(store.beliefs_map)
            )
            store.goals = [
                g for g in store.goals if lowered not in g.get("text", "").lower()
            ]
            store.memory = [
                m
                for m in store.memory
                if lowered not in m.get("text", "").lower()
                and lowered not in m.get("kind", "").lower()
            ]
            store.attention = {
                (a, v)
                for (a, v) in store.attention
                if lowered not in a.lower() and lowered not in v.lower()
            }
            store.beliefs_map = {
                k: c
                for k, c in store.beliefs_map.items()
                if lowered not in " ".join(k).lower()
            }
            store.archived_map = {
                k: c
                for k, c in store.archived_map.items()
                if lowered not in " ".join(k).lower()
            }
            store.rules = [r for r in store.rules if lowered not in r[0].lower()]
            store.chat_log = [c for c in store.chat_log if lowered not in c[1].lower()]
            after = (
                len(store.goals)
                + len(store.memory)
                + len(store.attention)
                + len(store.beliefs_map)
            )
            if after < before:
                store.dirty = True
                self.org.flush(force=True)
            return {"state": self.snapshot()}

    def goal(self, text):
        with self.lock:
            text = str(text).strip()
            if not text:
                raise WebError("goal needs text")
            self.org.add_goal(text)
            self.org.flush(force=True)
            return {"state": self.snapshot()}

    def priority(self, text):
        with self.lock:
            text = str(text).strip()
            if not text:
                raise WebError("priority needs a goal")
            store = self.org.store
            goals = store.goals
            matches = [
                i
                for i, g in enumerate(goals)
                if text.lower() in g.get("text", "").lower()
            ]
            if not matches:
                raise WebError(f"goal {text!r} not found")
            idx = matches[0]
            g = goals.pop(idx)
            goals.insert(0, g)
            store.dirty = True
            self.org.flush(force=True)
            return {"state": self.snapshot()}

    def attention(self, topic):
        with self.lock:
            topic = str(topic).strip()
            if topic:
                self.org.window.focus(topic)
                # Retain an explicit steering topic even when no belief matches it.
                if not self.org.window.pairs:
                    self.org.window.pairs.add(("attention", topic))
            else:
                self.org.window.focus(None)
            self.org.store.attention = self.org.window.pairs
            self.org.store.dirty = True
            self.org.flush(force=True)
        return {"state": self.snapshot()}

    def mode(self, mode):
        with self.lock:
            mode = str(mode).strip().lower()
            if mode not in ("wake", "sleep", "revive"):
                raise WebError("mode must be wake, sleep, or revive")
            if mode == "revive":
                events = [{"kind": "revive", "changed": self.org.revive()}]
            else:
                events = self.org.force_state(mode)
            self.org.flush(force=True)
            return {"events": events, "state": self.snapshot()}

    def save(self):
        with self.lock:
            self.org.flush(force=True)
            return {"state": self.snapshot()}

    def load(self):
        with self.lock:
            self.org.load()
            return {"state": self.snapshot()}

    def reset(self):
        with self.lock:
            name = self.name
            seed = self.root / "organism.scl"
            self.org.flush(force=True)
            target = nursery.organism_dir(self.root, name)
            if target.is_dir():
                shutil.rmtree(target)
            if not seed.exists():
                raise WebError("no organism seed found")
            nursery.create(self.root, name, seed)
            return self._swap(name, flush_old=False)

    def mutate(self, text):
        with self.lock:
            text = str(text).strip()
            if not text:
                text = "adapt"
            entry = {"kind": "seed", "text": text[:60]}
            ok, reason = extensions.validate(entry)
            if not ok:
                raise WebError(f"invalid mutation: {reason}")
            extensions.propose(self.extension_path, entry)
            return {"entry": entry, "state": self.snapshot()}

    def help(self):
        return {
            "commands": [
                {"name": "/help", "args": "", "desc": "toggle this help panel"},
                {
                    "name": "/mutate",
                    "args": " [text]",
                    "desc": "propose a mutation seed",
                },
                {
                    "name": "/cycle",
                    "args": "",
                    "desc": "advance one full wake-sleep cycle",
                },
                {
                    "name": "/rename",
                    "args": " <name>",
                    "desc": "rename the current organism",
                },
                {
                    "name": "/remember",
                    "args": " <text>",
                    "desc": "remember this text as an episode",
                },
                {
                    "name": "/forget",
                    "args": " <text>",
                    "desc": "forget entries containing text",
                },
                {"name": "/goal", "args": " <text>", "desc": "set a new goal"},
                {
                    "name": "/priority",
                    "args": " <goal>",
                    "desc": "move matching goal to front",
                },
                {
                    "name": "/attention",
                    "args": " <topic>",
                    "desc": "focus attention on a topic",
                },
                {
                    "name": "/mode",
                    "args": " <wake|sleep|revive>",
                    "desc": "set lifecycle mode",
                },
                {"name": "/save", "args": "", "desc": "persist organism state now"},
                {"name": "/load", "args": "", "desc": "reload organism from disk"},
                {
                    "name": "/reset",
                    "args": "",
                    "desc": "reset the current organism",
                },
                {
                    "name": "/chat",
                    "args": " <message>",
                    "desc": "send a normal chat message",
                },
            ],
            "state": self.snapshot(),
        }


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
                "/api/organisms": lambda: self.app.create_organism(
                    data.get("name", "")
                ),
                "/api/swap": lambda: self.app.swap(data.get("name", "")),
                "/api/rename": lambda: self.app.rename(data.get("name", "")),
                "/api/cycle": lambda: self.app.cycle(),
                "/api/remember": lambda: self.app.remember(data.get("text", "")),
                "/api/forget": lambda: self.app.forget(data.get("text", "")),
                "/api/goal": lambda: self.app.goal(data.get("text", "")),
                "/api/priority": lambda: self.app.priority(data.get("goal", "")),
                "/api/attention": lambda: self.app.attention(data.get("topic", "")),
                "/api/mode": lambda: self.app.mode(data.get("mode", "")),
                "/api/save": lambda: self.app.save(),
                "/api/load": lambda: self.app.load(),
                "/api/reset": lambda: self.app.reset(),
                "/api/mutate": lambda: self.app.mutate(data.get("text", "")),
                "/api/help": lambda: self.app.help(),
            }
            if path not in routes:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return self._json(HTTPStatus.OK, routes[path]())
        except (WebError, ValueError, TypeError) as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (OSError, RuntimeError):  # never expose local paths or prompts
            return self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "request failed"}
            )

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
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'",
        )
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
