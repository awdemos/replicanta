"""Local Glasshouse web front-end for Replicanta.

The server deliberately uses only the Python standard library.  It binds to
loopback by default, serves a small bundled client, and delegates every state
change to the same public organism/nursery APIs used by the TUI.
"""

import base64
import json
import secrets
import threading
import webbrowser
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from replicanta import (
    activity,
    extensions,
    fileutil,
    mud,
    nursery,
    speech,
    tui_commands,
    voice,
)
from replicanta import memory as memory_module
from replicanta.organism import Organism
from replicanta.web_static import APP_CSS, APP_HTML, APP_JS


class WebError(ValueError):
    """A safe client-facing request error."""


class Glasshouse:
    """Thread-safe adapter between HTTP requests and one live organism."""

    PUBLIC_API_GETS = ("/api/state", "/api/commands")

    def __init__(self, root, organism, spawn=None, respond=voice.respond, token=None):
        self.root = Path(root)
        self.org = organism
        self.spawn = dict(spawn or {})
        self.respond = respond
        self.token = token or secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self._self_talk = False
        self._listener = None
        self._camera = None
        self._last_frame = None
        # Hosted MUD games: host organism name -> MudGame.
        self._mud_games: dict[str, mud.MudGame] = {}
        # organism name -> host name for games this adapter has joined/started.
        self._mud_member_of: dict[str, str] = {}

    def auth_ok(self, request):
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[7:] == self.token
        return request.headers.get("X-Replicanta-Token") == self.token

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
            mud_state = self._mud_snapshot()
            persona_state = self._persona_snapshot()
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
                "memory_ranked": self._ranked_memory_snapshot(),
                "threads": [
                    {
                        "id": t.id,
                        "kind": t.kind,
                        "status": t.status,
                        "created_cycle": t.created_cycle,
                    }
                    for t in store.threads.values()
                ],
                "thread_results": list(store.thread_results),
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
                "persona": persona_state,
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

    def _mud_snapshot(self):
        """Build the MUD payload for the web client, or None when no game."""
        game = self._mud_game_for(self.org)
        if game is None:
            return None
        host = self._mud_host_for(self.org)
        actor_name = self.name
        actor = game.actors.get(actor_name)
        current = game.current_actor()
        return {
            "active": True,
            "host": host,
            "scenario": game.world.scenario.title,
            "premise": game.world.scenario.premise,
            "room": actor.room if actor else "",
            "inventory": list(actor.inventory) if actor else [],
            "roster": [
                {
                    "name": name,
                    "room": game.actors[name].room,
                    "inventory": list(game.actors[name].inventory),
                    "kind": game.actors[name].kind,
                    "is_you": name == actor_name,
                    "is_turn": name == current.name,
                }
                for name in game.turn_order
            ],
            "turn": current.name,
            "paused": game.paused,
            "finished": game.finished,
            "won": game.won,
            "log": [f"({a}) {c}" for a, c, _t in game.session.command_log[-20:]],
        }

    def _persona_snapshot(self):
        """Build the persona payload for the web client."""
        svc = getattr(self.org, "persona_service", None)
        if svc is None:
            return {"available": [], "active": None}
        active = svc.active()
        return {
            "available": svc.list(),
            "active": active["name"] if active else None,
        }

    def _ranked_memory_snapshot(self):
        """Return the top-ranked memories for the web client."""
        store = self.org.store
        query = " ".join(
            text for _role, text in store.chat_log[-4:]
        ) or "current situation"
        scorer = memory_module.MemoryScorer()
        ranked = scorer.rank(
            store.memory, query, top_k=8, current_cycle=store.cycle
        )
        return [
            {
                "cycle": m["cycle"],
                "kind": m["kind"],
                "text": m["text"],
                "importance": m.get("importance", 0.5),
            }
            for m in ranked
        ]

    def _mud_host_for(self, org):
        """Return the host organism name for the game org participates in."""
        name = org.dir_path.name
        if name in self._mud_games:
            return name
        return self._mud_member_of.get(name)

    def _mud_game_for(self, org):
        """Return the MudGame org participates in, or None."""
        host = self._mud_host_for(org)
        if host is None:
            return None
        return self._mud_games.get(host)

    def chat(self, text):
        text = str(text).strip()
        if not text or len(text) > 4000:
            raise WebError("message must contain 1-4000 characters")
        with self.lock:
            events = self.org.hear(text)
            reply = self.respond(self.org, text)
            self.org.flush(force=True)
            return {"reply": reply, "events": events, "state": self.snapshot()}

    def typing(self, data):
        """Record typing activity. Optionally nudges sleep near its boundary."""
        with self.lock:
            nudged = self.org.typing_activity()
            events = [{"kind": "typing", "nudged": nudged}]
            if nudged:
                events.extend(self.org.force_state("wake"))
            self.org.flush(force=True)
            return {"events": events, "state": self.snapshot()}

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
        elif args[0] == "get" and len(args) == 2 and not speech.download_voice(args[1]):
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
            elif name == "/export":
                try:
                    dest = self._export_chat(args[0] if args else None)
                    messages.append(f"chat exported to {dest}")
                except OSError as exc:
                    messages.append(f"export failed: {exc}")
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
                messages.append(self._mud_command(args))
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
        if args[0] == "get" and len(args) == 2 and not speech.download_voice(args[1]):
            return f"could not download voice {args[1]!r}"
        if args[0] == "get" and len(args) == 2:
            return f"downloaded voice {args[1]}"
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

    def _export_chat(self, path=None):
        """Write the full chat log to a markdown file. Returns the path."""
        org_name = self.name
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        if path:
            dest = fileutil.safe_path(Path.home(), path)
        else:
            dest = Path.home() / f"replicanta-chat-{org_name}-{timestamp}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            f"# Chat with {org_name}",
            "",
            f"Exported: {datetime.now(UTC).isoformat()}",
            f"Organism: {org_name}",
            f"Cycles: {self.org.store.cycle}",
            "",
        ]
        for role, text in self.org.store.chat_log:
            who = "You" if role == "user" else org_name
            lines.append(f"## {who}")
            lines.append("")
            lines.append(text)
            lines.append("")

        fileutil.atomic_write_text(dest, "\n".join(lines))
        return dest

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

    # -- MUD controller --------------------------------------------------------

    def _mud_load_scenario(self, slug):
        """Load a saved generated scenario by slug, or the built-in default."""
        if not slug or fileutil.slug(slug) != slug:
            return None
        directory = self.org.dir_path / "artifacts" / "mud" / "scenarios"
        path = directory / f"{slug}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return mud.validate_scenario(data)
            except Exception:  # noqa: BLE001
                return None
        default = mud.default_scenario()
        if fileutil.slug(default.title) == slug:
            return default
        return None

    def _mud_start(self, scenario=None, description=None):
        """Start a new MUD game hosted by the current organism."""
        host_name = self.name
        if host_name in self._mud_games:
            return "mud: a game is already running"
        if description:
            scenario = mud.generate_scenario(description, self.org)
            self._mud_save_scenario(scenario)
        elif scenario is None:
            loaded = self._mud_load_scenario_from_session()
            scenario = loaded or mud.default_scenario()
        game = mud.MudGame(scenario)
        # The web host plays as the user; the legacy default "organism"
        # actor is replaced by the host organism.
        game.remove_actor("organism")
        game.add_actor(host_name, kind="user")
        self._mud_games[host_name] = game
        self._mud_member_of[host_name] = host_name
        self.org.store.save_mud_session(game.session)
        return f"mud: entered {game.world.scenario.title}"

    def _mud_load_scenario_from_session(self):
        """Resume a saved session's scenario when available."""
        session = self.org.store.load_mud_session()
        if session is None:
            return None
        return self._mud_load_scenario(session.scenario_id)

    def _mud_save_scenario(self, scenario):
        """Persist a generated scenario to artifacts/mud/scenarios/<slug>.json."""
        try:
            directory = self.org.dir_path / "artifacts" / "mud" / "scenarios"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{fileutil.slug(scenario.title)}.json"
            fileutil.atomic_write_text(
                path, json.dumps(mud.scenario_to_json(scenario), indent=1)
            )
        except OSError:
            pass

    def _mud_join(self, host_name):
        """Add the current organism to a hosted game."""
        game = self._mud_games.get(host_name)
        if game is None:
            return f"mud: no hosted game named {host_name!r}"
        member_name = self.name
        if member_name in self._mud_member_of:
            return "mud: already in a game; /mud leave first"
        if member_name in game.actors:
            return "mud: already in this game"
        game.add_actor(member_name, kind="organism")
        self._mud_member_of[member_name] = host_name
        return f"mud: joined {host_name}'s game as {member_name}"

    def _mud_leave(self):
        """Remove the current organism from its game."""
        name = self.name
        host = self._mud_member_of.get(name)
        if host is None:
            return "mud: not in a game"
        game = self._mud_games.get(host)
        if game is not None:
            game.remove_actor(name)
        del self._mud_member_of[name]
        if host == name:
            # Host leaving ends the game for everyone.
            for member in list(self._mud_member_of):
                if self._mud_member_of[member] == host:
                    del self._mud_member_of[member]
            self._mud_games.pop(host, None)
            return "mud: game ended"
        return "mud: left the game"

    def _mud_stop(self):
        """Stop the current organism's hosted game."""
        name = self.name
        if name not in self._mud_games:
            return "mud: you are not hosting a game"
        game = self._mud_games.pop(name)
        for member in list(self._mud_member_of):
            if self._mud_member_of[member] == name:
                del self._mud_member_of[member]
        return f"mud: {game.world.scenario.title} ended"

    def _mud_command(self, args):
        """Dispatch /mud subcommands for the web UI."""
        if not args:
            if self.name in self._mud_games:
                return self._mud_stop()
            return self._mud_start()

        sub = args[0]
        if sub == "start":
            return self._mud_start()
        if sub == "join" and len(args) == 2:
            return self._mud_join(args[1])
        if sub == "leave":
            return self._mud_leave()
        if sub == "stop":
            return self._mud_stop()

        game = self._mud_game_for(self.org)
        if game is None:
            return "mud: no game running (start with /mud)"

        if sub in ("map", "story", "quest"):
            render = {
                "map": mud.render_map,
                "story": mud.render_story,
                "quest": mud.render_quest,
            }[sub]
            return render(game)
        if sub == "pause":
            if game.paused:
                return "mud: already paused"
            game.paused = True
            return "mud: paused"
        if sub == "resume":
            if not game.paused:
                return "mud: not paused"
            game.paused = False
            return "mud: resumed"
        if sub == "step":
            return self._mud_step(game)
        if sub == "reset":
            scenario = game.world.scenario
            host = self._mud_host_for(self.org)
            self._mud_stop()
            if host == self.name:
                return self._mud_start(scenario=scenario)
            return "mud: reset the scenario"
        if sub == "scenario":
            description = " ".join(args[1:]).strip()
            if not description:
                return "/mud scenario needs a description"
            host = self._mud_host_for(self.org)
            if host != self.name:
                return "mud: only the host can start a new scenario"
            self._mud_stop()
            return self._mud_start(description=description)
        return "/mud [start|join <name>|leave|stop|map|story|quest|pause|resume|step|reset|scenario <desc>]"

    def _mud_step(self, game):
        """Execute one organism turn."""
        actor = game.current_actor()
        if actor.kind != "organism":
            return f"mud: it is {actor.name}'s turn (not an organism)"
        org = self._mud_organism_for(actor.name)
        if org is None:
            return f"mud: cannot find organism {actor.name}"
        choice = mud.choose_action(game, org=org, actor_name=actor.name)
        result = game.act_event(choice.command or "look", actor_name=actor.name)
        host = self._mud_host_for(self.org)
        if host and host in self._mud_games:
            self._mud_games[host].session = game.session
        self.org.store.save_mud_session(game.session)
        return f"mud: {actor.name} {result.text}"

    def _mud_organism_for(self, name):
        """Load an organism by name for taking a MUD turn."""
        if self.org.dir_path.name == name:
            return self.org
        org_dir = nursery.organism_dir(self.root, name)
        if not org_dir.exists():
            return None
        try:
            org = Organism(org_dir, **self.spawn)
            org.load()
            return org
        except Exception:  # noqa: BLE001
            return None

    def mud_act(self, text):
        """Apply a player command while it is their organism's turn."""
        with self.lock:
            game = self._mud_game_for(self.org)
            if game is None:
                raise WebError("no active MUD game")
            actor_name = self.name
            if game.current_actor_name() != actor_name:
                raise WebError(f"it is {game.current_actor_name()}'s turn")
            command = mud.parse_player_command(text)
            if command is None:
                raise WebError("not a MUD command")
            result = game.act_event(command, actor_name=actor_name)
            host = self._mud_host_for(self.org)
            if host and host in self._mud_games:
                self._mud_games[host].session = game.session
            self.org.store.save_mud_session(game.session)
            messages = [result.text]
            # Auto-step one organism turn if the next actor is an organism.
            if not game.finished and not game.paused:
                next_actor = game.current_actor()
                if next_actor.kind == "organism" and next_actor.name != actor_name:
                    org = self._mud_organism_for(next_actor.name)
                    if org is not None:
                        choice = mud.choose_action(game, org=org, actor_name=next_actor.name)
                        next_result = game.act_event(
                            choice.command or "look", actor_name=next_actor.name
                        )
                        messages.append(f"{next_actor.name}: {next_result.text}")
                        if host and host in self._mud_games:
                            self._mud_games[host].session = game.session
                        self.org.store.save_mud_session(game.session)
            return {"messages": messages, "state": self.snapshot()}


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
        if not self.app.auth_ok(self):
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
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
                "/api/mud-act": lambda: self.app.mud_act(data.get("text", "")),
                "/api/typing": lambda: self.app.typing(data),
            }
            if path not in routes:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return self._json(HTTPStatus.OK, routes[path]())
        except (WebError, ValueError, TypeError) as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (OSError, RuntimeError):
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
    app = Glasshouse(root, organism, spawn=spawn)
    server = make_server(app, host, port)
    url = f"http://{host}:{server.server_port}"
    print(f"Replicanta Glasshouse: {url}")
    print(f"Authorization token: {app.token}")
    if open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(f"{url}/#token={app.token}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.glasshouse.org.flush(force=True)
        server.server_close()
