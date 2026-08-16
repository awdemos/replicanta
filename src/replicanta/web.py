"""Local Glasshouse web front-end for Replicanta.

The server deliberately uses only the Python standard library.  It binds to
loopback by default, serves a small bundled client, and delegates every state
change to the same public organism/nursery APIs used by the TUI.
"""

import base64
import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from replicanta import activity, extensions, nursery, speech, tui_commands, voice
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
        self._self_talk = False
        self._listener = None
        self._camera = None
        self._last_frame = None

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
            mud_state = None
            try:
                session = self.org.load_mud_session()
                if session is not None:
                    mud_state = session.to_json()
            except Exception:  # noqa: BLE001 — MUD is optional display data
                pass
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
                    "insane": bool(store.insane),
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
                "activity": activity.summary_lines(store),
                "speech": {
                    "enabled": speech.enabled,
                    "available": speech.available(),
                    "voices": speech.list_voices(),
                    "current": speech.voice_name(),
                },
                "git_enabled": getattr(self.org, "git_probe", None) is not None,
                "self_talk": self._self_talk,
                "mud": mud_state,
                "sight": self._last_frame,
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
                    self.org.window.focus(focus)
                    self.org.store.attention = self.org.window.pairs
                else:
                    self.org.store.attention = set()
                self.org.store.dirty = True
            if "voice" in data:
                self._apply_voice(str(data["voice"]))
            if "self_talk" in data:
                self._self_talk = bool(data["self_talk"])
            if "git" in data:
                self._apply_git(str(data["git"]))
            self.org.flush(force=True)
            return self.snapshot()

    def _apply_voice(self, spec):
        args = spec.split()
        if not args:
            return
        if args[0] == "on":
            speech.set_enabled(True)
        elif args[0] == "off":
            speech.set_enabled(False)
        elif args[0] == "use" and len(args) == 2:
            if not speech.set_voice(args[1]):
                raise WebError(f"no voice named {args[1]!r}")
        elif args[0] == "get" and len(args) == 2:
            if not speech.download_voice(args[1]):
                raise WebError(f"could not download voice {args[1]!r}")

    def _apply_git(self, spec):
        if spec == "on":
            self.org.git_enable()
        elif spec == "off":
            self.org.git_disable()

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

    def command(self, text):
        text = str(text).strip()
        if not text.startswith("/"):
            raise WebError("commands must start with /")
        parts = text.split()
        name = parts[0]
        args = parts[1:]
        messages = []
        with self.lock:
            if name == "/chaos":
                if len(args) != 1:
                    raise WebError("/chaos needs a number 0-1")
                self.settings({"chaos": float(args[0])})
                messages.append(f"chaos: {self.org.store.chaos:.2f}")
            elif name == "/focus":
                self.settings({"focus": " ".join(args)})
                if args:
                    messages.append(f"attention locked on {' '.join(args)}")
                else:
                    messages.append("attention floating free")
            elif name == "/sleep":
                self.lifecycle("sleep")
                messages.append("sleeping")
            elif name == "/wake":
                self.lifecycle("wake")
                messages.append("waking")
            elif name == "/revive":
                if self.org.revive():
                    messages.append("revived: the organism stirs back into existence.")
                else:
                    messages.append(
                        f"/revive: it is not faded (state {self.org.lifecycle.state})."
                    )
            elif name == "/stats":
                m = self.org.metrics()
                s = self.org.store
                messages.append(
                    f"stats: beliefs={m.belief_count} rules={m.rule_count} "
                    f"depth={m.total_depth} score={m.score():.1f}"
                )
                messages.append(
                    f"mental: arousal={s.arousal:.2f} "
                    f"rationality={s.rationality:.2f} "
                    f"irrationality={s.irrationality:.2f} "
                    f"insane={s.insane}"
                )
                messages.extend(activity.summary_lines(s))
            elif name == "/save":
                self.org.flush(force=True)
                messages.append("saved")
            elif name == "/think":
                thought = voice.narrate(self.org)
                if thought:
                    self.org.store.record_chat("org", thought)
                    speech.say(thought)
                messages.append(thought or "nothing to say right now")
            elif name == "/self-talk":
                self._self_talk = not self._self_talk
                if self._self_talk and self.org.lifecycle.state == "wake":
                    q = voice.self_ask(self.org)
                    a = voice.self_answer(self.org, q)
                    self.org.store.record_chat("org", q)
                    self.org.store.record_chat("org", f"  ↳ {a}")
                    speech.say(q)
                    speech.say(a)
                    messages.append(f"self-talk on — {q}")
                else:
                    messages.append("self-talk off")
            elif name == "/voice":
                messages.append(self._voice_command(args))
            elif name == "/listen":
                messages.append(self._listen_command())
            elif name == "/look":
                messages.append(self._look_command())
            elif name == "/mud":
                messages.append("/mud is only available in the TUI.")
            elif name == "/persona":
                messages.append(self._persona_command(args))
            elif name == "/modules":
                messages.append("Modules are managed in the TUI (F9).")
            elif name == "/approve":
                entry = extensions.approve(self.extension_path)
                if entry:
                    self.org.store.remember("skill", f"patch applied ({entry['kind']})")
                    messages.append(f"patch applied ({entry['kind']})")
                else:
                    messages.append("no pending patch")
            elif name == "/reject":
                entry = extensions.reject(self.extension_path)
                if entry:
                    self.org.store.remember("skill", f"patch rejected ({entry['kind']})")
                    messages.append(f"patch rejected ({entry['kind']})")
                else:
                    messages.append("no pending patch")
            elif name == "/revert":
                entry = extensions.revert_last(self.extension_path)
                if entry:
                    self.org.store.remember("skill", f"patch reverted ({entry['kind']})")
                    messages.append(f"patch reverted ({entry['kind']})")
                else:
                    messages.append("no applied patches yet")
            elif name == "/auto-apply":
                if args and args[0] in ("on", "off"):
                    self.org.store.auto_apply_patches = args[0] == "on"
                    self.org.store.dirty = True
                state = "on" if self.org.store.auto_apply_patches else "off"
                messages.append(f"auto-apply patches: {state}")
            elif name == "/new":
                new_name = args[0] if args else nursery.next_name(self.root)
                self.create_organism(new_name)
                messages.append(f"created and swapped to {new_name}")
            elif name == "/swap":
                if len(args) != 1:
                    raise WebError("/swap needs a name — /organisms to list")
                self.swap(args[0])
                messages.append(f"swapped to {args[0]}")
            elif name == "/organisms":
                names = nursery.list_organisms(self.root)
                current = self.name
                listing = ", ".join(f"*{n}" if n == current else n for n in names) or "(none)"
                messages.append(f"organisms: {listing}  (* = current)")
            elif name == "/group":
                messages.append("Group chat is only available in the TUI.")
            elif name == "/reload":
                self.org.hooks.reload()
                count = len(self.org.hooks.scripts)
                messages.append(f"lua hooks reloaded ({count} script{'s' if count != 1 else ''})")
            elif name == "/lua":
                if len(args) != 1:
                    names = ", ".join(s.name for s in self.org.hooks.scripts)
                    raise WebError(f"/lua needs a script name (scripts/: {names or 'none'})")
                messages.append(str(self.org.hooks.run(args[0], self.org)))
            elif name == "/git":
                messages.append(self._git_command(args))
            elif name == "/help":
                messages.append(tui_commands.help_text())
            elif name == "/quit":
                messages.append("Use the browser tab close button to exit.")
            else:
                raise WebError(f"unknown command {name} (try /help)")
            self.org.flush(force=True)
            return {"messages": messages, "state": self.snapshot()}

    def _voice_command(self, args):
        if not args or args[0] in ("on", "off"):
            if args:
                speech.set_enabled(args[0] == "on")
            else:
                speech.set_enabled(not speech.enabled)
            state = "on" if speech.enabled else "off"
            if speech.enabled:
                if speech.available():
                    speech.say("I can speak now.")
                    return f"spoken voice {state}"
                return f"spoken voice {state}, but no piper model available"
            return f"spoken voice {state}"
        if args[0] == "list":
            voices = speech.list_voices()
            active = speech.voice_name()
            listing = ", ".join(f"*{v}" if v == active else v for v in voices) or "(none)"
            return f"voices: {listing}  (* = active)"
        if args[0] == "use" and len(args) == 2:
            if speech.set_voice(args[1]):
                speech.say("This is my new voice.")
                return f"voice: {speech.voice_name()}"
            return f"no voice named {args[1]!r}"
        if args[0] == "get" and len(args) == 2:
            if speech.download_voice(args[1]):
                return f"downloaded voice {args[1]}"
            return f"could not download voice {args[1]!r}"
        return "/voice [on|off|list|use name|get name]"

    def _git_command(self, args):
        if not args or args[0] == "status":
            return self.org.git_status()
        if args[0] == "on":
            self.org.git_enable()
            return "git sensing on"
        if args[0] == "off":
            self.org.git_disable()
            return "git sensing off"
        return "/git [on|off|status]"

    def _persona_command(self, args):
        svc = getattr(self.org, "persona_service", None)
        if svc is None:
            return "persona service unavailable"
        if not args or args[0] == "list":
            active = svc.active()
            names = svc.list()
            line = "personas: " + ", ".join(
                f"*{n}" if active and active["name"] == n else n for n in names
            )
            return line
        if args[0] == "off":
            svc.deactivate()
            return "persona cleared"
        svc.activate(args[0])
        return f"persona: {args[0]}"

    def _listen_command(self):
        try:
            from replicanta import listen

            if self._listener is None:
                self._listener = listen.Listener()
            recording, audio = self._listener.toggle()
            if not recording and audio is not None:
                text = self._listener.transcribe(audio)
                if text:
                    self.org.hear(text)
                    self.org.store.record_chat("user", text)
                    return f"heard: {text}"
                return "heard nothing"
            return "listening..."
        except Exception as exc:  # noqa: BLE001 — audio is optional hardware
            return f"listening unavailable: {exc}"

    def _look_command(self):
        try:
            from replicanta import camera

            if self._camera is None:
                self._camera = camera.Camera()
            frame = self._camera.grab()
            if frame:
                self._last_frame = base64.b64encode(frame).decode("ascii")
                return "captured a frame"
            return "no camera frame"
        except Exception as exc:  # noqa: BLE001 — camera is optional hardware
            return f"camera unavailable: {exc}"


class GlasshouseHandler(BaseHTTPRequestHandler):
    server_version = "ReplicantaGlasshouse/0.1"

    @property
    def app(self):
        return self.server.glasshouse

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            return self._json(HTTPStatus.OK, self.app.snapshot())
        if path == "/api/commands":
            return self._json(
                HTTPStatus.OK,
                [
                    {"name": name, "usage": usage, "description": description}
                    for name, usage, description, _category in tui_commands.COMMANDS
                ],
            )
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
                "/api/command": lambda: self.app.command(data.get("command", "")),
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
