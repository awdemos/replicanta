"""Per-subsystem controllers for the TUI: each controller owns one domain's
behavior — MudController the dungeon crawl, DoomController nano-doom play,
VoiceController inner-voice health and piper downloads. The Textual app stays
the composition root: it wires the controllers, keeps the @work worker/thread
boundaries (workers must live on the App to reach run_worker), and delegates.
Controllers reach the app only for UI primitives (log lines, timers, activity
label, call_from_thread) and always by dynamic attribute lookup, so test
monkeypatching and organism swaps keep working."""

import contextlib
import logging
from datetime import UTC, datetime

from textual.widgets import Static, TabbedContent

from replicanta import mud, speech, voice
from replicanta.tui_views import (
    STYLE_DIM,
    STYLE_DREAM,
    STYLE_LEARNED,
    STYLE_SELF,
    STYLE_WARN,
)

logger = logging.getLogger(__name__)

MUD_TURN_DELAY = 4.0  # seconds between dungeon moves


def doom_player_command(text):
    """User chat input -> normalized nano-doom command, or None if not a move."""
    if not text:
        return None
    words = [w for w in text.strip().lower().split()]
    if not words:
        return None
    # Obsolete: prefer arrow keys when the DOOM pane is active so chat typing
    # is not confused with movement commands.
    if len(words) == 1 and words[0] in ("shoot", "look", "start", "stop", "status"):
        return words[0]
    return None


def extract_doom_command(reply):
    """Look for a line that looks like doom.command(...) and return the inner arg.
    Also tolerate a bare move word as a fallback for sloppy model output."""
    if not reply:
        return None
    valid = {"w", "a", "s", "d", "q", "e", "shoot", "use"}
    for line in reply.strip().splitlines():
        line = line.strip()
        if line.startswith("doom.command("):
            if line.endswith(")"):
                inner = line[len("doom.command(") : -1].strip()
                inner = inner.strip('"').strip("'")
                if inner in valid:
                    return inner
            continue
        # Fallback: a line that is just one of the valid moves.
        lowered = line.lower()
        if lowered in valid:
            return lowered
        # 'shoot' may appear as a single word anywhere.
        if lowered == "shoot":
            return "shoot"
    return None


class MudController:
    """Owns the organism's dungeon crawl: the /mud lifecycle (start/stop/
    reset/scenario), session and scenario persistence on the organism store,
    and the turn heartbeat (choose -> apply -> schedule). State is plain
    attributes so headless tests can seat a game directly."""

    def __init__(self, app):
        self._app = app
        self.game = None
        self.hint = None  # one-shot user nudge for the next move
        self.paused = True  # start paused; the human plays the MUD
        self.thinking = False  # a move-choice worker is in flight
        self.turn_gen = 0  # bumped by user moves/hints: stales in-flight

    # -- /mud dispatch --------------------------------------------------------
    def command(self, args):
        """Dispatch /mud subcommands: bare toggles, the rest control or
        inspect the running game."""
        if not args:
            self.toggle()
            return
        sub = args[0]
        if sub in ("map", "story", "quest"):
            game = self.game
            if game is None:
                self._app._append_log(f"/mud {sub}: no game running (start with /mud)", STYLE_DIM)
                return
            render = {
                "map": mud.render_map,
                "story": mud.render_story,
                "quest": mud.render_quest,
            }[sub]
            self._app._append_log(render(game), STYLE_DREAM)
        elif sub == "pause":
            if self.game is None:
                self._app._append_log("/mud pause: no game running.", STYLE_DIM)
            elif self.paused:
                self._app._append_log("mud: already paused (/mud resume)", STYLE_DIM)
            else:
                self.paused = True
                self._app._append_log(
                    "— the dungeon holds its breath (paused; /mud resume, /mud step, or type a command) —",
                    STYLE_DIM,
                    stamp=True,
                )
                self._app.refresh_status()
        elif sub == "resume":
            if self.game is None:
                self._app._append_log("/mud resume: no game running.", STYLE_DIM)
            elif not self.paused:
                self._app._append_log("mud: not paused.", STYLE_DIM)
            else:
                self.paused = False
                self._app._append_log("— the dungeon stirs again —", STYLE_DIM, stamp=True)
                self._app.refresh_status()
                self.next_turn()
        elif sub == "step":
            self.step()
        elif sub == "reset":
            self.reset()
        elif sub == "scenario":
            description = " ".join(args[1:]).strip()
            if not description:
                self._app._append_log(
                    "/mud scenario needs a description, e.g. /mud scenario a haunted space station",
                    STYLE_DIM,
                )
                return
            self.scenario(description)
        else:
            self._app._append_log(
                "/mud [map|story|quest|pause|resume|step|reset|scenario <description>]",
                STYLE_DIM,
            )

    def toggle(self):
        """/mud: start or stop the organism's dungeon crawl. The world is
        deterministic; the voice picks the moves (wanderer fallback)."""
        if self.game is not None:
            self.stop()
            return
        self.start()

    def start(self, scenario=None, fresh=False):
        """Begin a game: with the given scenario, else resuming the saved
        session when one exists, else the default dungeon."""
        session = None
        if scenario is None and not fresh:
            scenario, session = self.restore()
        game = mud.MudGame(scenario)
        if session is not None:
            # the session tracks map/story but not room/inventory: replay
            # the logged commands to bring back the exact game state
            for actor, command, _turn in session.command_log:
                game.act_event(command, actor_name=actor)
            game.session = session
        self.game = game
        self.paused = True
        if session is not None:
            self._app._append_log(
                f"— the organism returns to {game.scenario.title} (turn {game.turns}) —",
                STYLE_DREAM,
                stamp=True,
            )
        else:
            self._app._append_log(
                "— the dungeon opens for you; the organism stands beside you as a companion —",
                STYLE_DREAM,
                stamp=True,
            )
            self._app._append_log(mud.build_premise(self._app.org, game.scenario), STYLE_DREAM)
        self._app._append_log(game.look(), STYLE_DREAM)
        self._app.org.store.remember("mud", f"started {game.scenario.title}")
        self.save_session(game)
        self._app.refresh_status()

    def stop(self):
        """End the current game, persisting its session for a later resume."""
        game, self.game = self.game, None
        self.paused = False
        self.save_session(game)
        self._app._append_log(
            f"— the dungeon fades (stopped after {game.turns} turns) —",
            STYLE_DIM,
            stamp=True,
        )
        self._app.org.store.remember("mud", f"left {game.scenario.title} after {game.turns} turns")
        self._app.refresh_status()

    def reset(self):
        """Restart the current scenario fresh, discarding the session."""
        game = self.game
        if game is None:
            self._app._append_log("/mud reset: no game running.", STYLE_DIM)
            return
        scenario = game.scenario
        self.game = None
        self.paused = False
        self._app._append_log("— the dungeon resets —", STYLE_DIM, stamp=True)
        self.start(scenario=scenario, fresh=True)

    def step(self):
        """/mud step: exactly one organism turn while paused."""
        if self.game is None:
            self._app._append_log("/mud step: no game running.", STYLE_DIM)
            return
        if not self.paused:
            self._app._append_log("auto-turns are running — /mud pause first", STYLE_DIM)
            return
        if self.thinking:
            return  # a move is already being chosen
        self.thinking = True
        self._app._mud_turn()

    def scenario(self, description):
        """/mud scenario <description>: stop the current game and dream up
        a new scenario with the voice (off the UI thread)."""
        if self.game is not None:
            self.stop()
        self._app._append_log(f"dreaming up a scenario: {description}…", STYLE_DIM)
        self._app._mud_scenario_worker(description)

    def scenario_worker(self, description):
        self._app.call_from_thread(self._app.set_activity, "MUD dreaming")
        try:
            scenario = mud.generate_scenario(description, self._app.org)
        except Exception as exc:  # noqa: BLE001 — voice offline etc.
            self._app.call_from_thread(self._app._append_log, f"/mud scenario failed: {exc}", STYLE_WARN)
            return
        finally:
            self._app.call_from_thread(self._app.clear_activity)
        self._app.call_from_thread(self.start_scenario, scenario)

    def start_scenario(self, scenario):
        self.save_scenario(scenario)
        self._app._append_log(f"new scenario: {scenario.title}", STYLE_LEARNED, stamp=True)
        self.start(scenario=scenario, fresh=True)

    # -- chat routing ---------------------------------------------------------
    def route_text(self, text):
        """Chat routing for the MUD: a direct move applies instantly (True =
        consumed); prose becomes a one-shot hint for the next organism move
        and falls through to normal chat handling (False)."""
        if self.game is None:
            return False
        command = mud.parse_player_command(text)
        if command is not None:
            # a direct move: execute now, not a hint, not chat. Bump
            # the turn generation so an organism move chosen before
            # this command cannot land after it.
            self.turn_gen += 1
            self.apply(self.game, command, actor="user")
            return True
        self.turn_gen += 1  # hints invalidate in-flight moves too
        self.hint = text  # shout a nudge into the next move
        return False

    # -- persistence ----------------------------------------------------------
    def artifacts_dir(self):
        return self._app.org.dir_path / "artifacts"

    def save_session(self, game=None):
        """Persist the session after every turn and on stop via the
        organism-side BeliefStore."""
        game = game or self.game
        if game is None:
            return
        self._app.org.store.save_mud_session(game.session)

    def load_session(self):
        return self._app.org.store.load_mud_session()

    def restore(self):
        """(scenario, session) from disk for resume, or (None, None) to
        start fresh: a finished or scenario-less session is not resumed."""
        session = self.load_session()
        if session is None or session.outcome is not None:
            return None, None
        scenario = self.load_scenario(session.scenario_id)
        if scenario is None:
            return None, None
        return scenario, session

    def load_scenario(self, slug):
        """A saved generated scenario by slug, or the built-in default —
        thin wrapper over ``mud.load_scenario`` that only translates errors
        into the TUI log. The slug comes from a resumed session on disk, so
        the domain layer re-validates it before touching the filesystem."""
        try:
            return mud.load_scenario(slug, self.artifacts_dir())
        except (OSError, ValueError) as exc:
            self._app._append_log(f"mud: couldn't load scenario {slug} ({exc})", STYLE_WARN)
            return mud.default_scenario_for_slug(slug)

    def save_scenario(self, scenario):
        """Save a generated scenario via the domain layer, rendering the
        result message (or the OSError) into the TUI log."""
        try:
            self._app._append_log(mud.save_scenario(scenario, self.artifacts_dir()), STYLE_DIM)
        except OSError as exc:
            self._app._append_log(f"mud: couldn't save scenario ({exc})", STYLE_WARN)

    # -- turns ----------------------------------------------------------------
    def turn(self):
        """Choose the organism's next move (runs in the app's worker
        thread; the @work boundary lives on the app)."""
        game = self.game
        if game is None:
            self.thinking = False
            return
        self._app.call_from_thread(self._app.set_activity, "MUD thinking")
        hint, self.hint = self.hint, None
        gen = self.turn_gen
        try:
            command, reason = mud.choose_action(game, hint=hint, rng=self._app._rng, org=self._app.org)
            self._app.call_from_thread(self.apply, game, command, "organism", gen, reason)
        except Exception as exc:  # noqa: BLE001
            self._app.call_from_thread(self._app._worker_error, "MUD turn", exc)
        finally:
            self._app.call_from_thread(self._app.clear_activity)

    def apply(self, game, command, actor="organism", gen=None, reason=None):
        if actor == "organism":
            self.thinking = False
        if self.game is not game:
            return  # stopped (or restarted) meanwhile
        if actor == "organism" and gen is not None and gen != self.turn_gen:
            # a user move (or hint) landed while this move was being
            # chosen — it was picked from a world that no longer
            # exists; dropping it keeps the heartbeat honest
            self._app._append_log("> (the organism hesitates — the moment passed)", STYLE_DIM)
            self.schedule()
            return
        if actor == "organism" and reason:
            self._app._append_log(reason, STYLE_DIM)
        self._app._append_log(f"> {command}", STYLE_SELF)
        result = game.act_event(command, actor_name=actor)
        self._app._append_log(result.text, STYLE_DREAM)
        if result.plot:
            self._app._append_log(result.plot, STYLE_LEARNED)
        if game.finished:
            outcome = "won" if game.won else "lost"
            self._app._append_log(
                f"— {game.scenario.title} is {outcome} in {game.turns} turns —",
                STYLE_LEARNED,
                stamp=True,
            )
            self._app.org.store.remember("mud", f"{outcome} {game.scenario.title} in {game.turns} turns")
            self.save_session(game)
            self.game = None
            self.paused = False
            self._app.refresh_status()
            return
        self.save_session(game)
        if actor == "organism":
            # the organism's heartbeat: user commands execute instantly and
            # never schedule (a timer is pending, or the game is paused)
            self.schedule()

    def schedule(self):
        if not self.paused:
            self._app.set_timer(MUD_TURN_DELAY, self.next_turn)

    def next_turn(self):
        if self.game is not None and not self.paused and not self.thinking:
            self.thinking = True
            self._app._mud_turn()


class DoomController:
    """Owns nano-doom play: /doom dispatch, the DOOM pane rendering, the
    entity's auto-play turn loop, and the arrow/space key commands. The
    app's action_doom_* bindings are thin delegates so Textual dispatch and
    test monkeypatching stay on the app."""

    def __init__(self, app):
        self._app = app
        self._text = ""

    def toggle(self):
        loader = getattr(self._app.org, "module_loader", None)
        svc = loader.registry.get("doom") if loader is not None else None
        if svc is None:
            return
        active = self._app.query_one(TabbedContent).active
        if active != "doom-pane":
            self._app.action_show_tab("doom-pane")
            # If no game is running, start one immediately when opening the pane.
            if not svc.running():
                self.command(["start"])
            # Once the game is started, kick the entity into auto-play.
            if svc.running():
                self._app.set_timer(0.3, self.take_turn)
            return
        # Already on the DOOM pane: toggling again stops the game and leaves the pane.
        if svc.running():
            self.command(["stop"])

    def key_command(self, cmd):
        loader = getattr(self._app.org, "module_loader", None)
        svc = loader.registry.get("doom") if loader is not None else None
        if svc is None:
            return
        # When on the DOOM pane, arrow/space keys drive the game; otherwise ignore.
        active = self._app.query_one(TabbedContent).active
        if active != "doom-pane":
            return
        # If no game is running, start one automatically on the first keypress.
        if not svc.running():
            if cmd == "stop":
                return
            self.command(["start"])
        if not svc.running():
            return
        # Human took manual control: cancel any queued auto-turn and run the
        # command immediately so the player feels in charge.
        self.cancel_auto()
        if cmd != "start":
            self.command([cmd])
        # After the human moves, let the entity respond and take its turn.
        if svc.running():
            self._app.set_timer(0.3, self.take_turn)

    def cancel_auto(self):
        """Cancel pending auto-play timers so manual control wins."""
        for timer in list(getattr(self._app, "_timers", [])):
            if getattr(timer, "_callback", None) is self.take_turn:
                timer.stop()

    def chat_command(self, text):
        """User chat during a game counts as direction; cancel auto-play
        so the entity responds to the user rather than stacking turns.
        Returns True when the line was consumed as a doom command."""
        loader = getattr(self._app.org, "module_loader", None)
        doom_svc = loader.registry.get("doom") if loader is not None else None
        doom_running = False
        if doom_svc is not None:
            try:
                doom_running = bool(doom_svc.running())
            except Exception:  # noqa: BLE001
                doom_running = False
        if doom_running:
            self.cancel_auto()
            command = doom_player_command(text)
            if command is not None:
                self.command([command])
                return True
        return False

    def mirror_thought(self, reply):
        """Append entity reasoning to the DOOM pane's thought stream when a
        game is running (called from the response worker thread)."""
        loader = getattr(self._app.org, "module_loader", None)
        doom_svc = loader.registry.get("doom") if loader is not None else None
        try:
            if doom_svc is not None and doom_svc.running():
                self._app.call_from_thread(self.set_thought, reply)
        except Exception:
            logger.warning("doom thought mirror failed", exc_info=True)

    def command(self, args):
        """Dispatch /doom subcommands: bare = status, start/stop, or direct
        movement/shoot while a game is running."""
        loader = getattr(self._app.org, "module_loader", None)
        if loader is None:
            self._app._append_log("module loader unavailable", STYLE_WARN)
            return
        svc = loader.registry.get("doom")
        if svc is None:
            self._app._append_log("nano-doom module not loaded (enable it via /modules)", STYLE_WARN)
            return
        commands = loader.registry.get("commands")
        if commands is None:
            self._app._append_log("command service unavailable", STYLE_WARN)
            return
        try:
            result = commands.dispatch("/doom", args if args else [])
        except Exception as exc:  # noqa: BLE001
            self._app._append_log(f"doom command failed: {exc}", STYLE_WARN)
            return
        # Render into the dedicated DOOM pane instead of the chat log.
        lines = str(result or "").splitlines()
        if lines:
            self._text = "\n".join(lines)
            doom = self._app._safe_query("#doom", Static)
            if doom is not None:
                doom.update(self._text)
        # Switch to the DOOM pane when a game starts or renders.
        if args and args[0] in ("start", "status"):
            self._app.action_show_tab("doom-pane")
        # Nudge the organism to observe any game frame it produced.
        try:
            status = svc.status()
            self._app.org.store.add(("doom", "frame", "running" if svc.running() else "idle"), 0.9)
            self._app.org.store.remember("doom", status[:200])
        except Exception as exc:  # noqa: BLE001
            logger.warning("doom observe failed: %s", exc)
        # After starting, wake the entity so it immediately plays and the user
        # can watch its streaming thought process. Multiple staggered timers
        # bootstrap auto-play even if the first generation is slow or fails.
        if args and args[0] == "start" and svc.running():
            self._app.set_timer(0.2, self.take_turn)
            self._app.set_timer(0.7, self.take_turn)
            self._app.set_timer(1.5, self.take_turn)

    def refresh(self):
        """Refresh the DOOM pane when a game is running."""
        loader = getattr(self._app.org, "module_loader", None)
        svc = loader.registry.get("doom") if loader is not None else None
        if svc is None or not svc.running():
            return
        try:
            text = svc.status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("doom refresh failed: %s", exc)
            return
        if text != self._text:
            self._text = text
            doom = self._app._safe_query("#doom", Static)
            if doom is not None:
                doom.update(self._text)

    def set_thought(self, text):
        """Append entity reasoning to the DOOM pane's thought stream."""
        thoughts = self._app._safe_query("#doom-thoughts", Static)
        if thoughts is None:
            return
        # Normalize: strip a stale live-typing "> " prefix before re-stamping.
        current = str(getattr(thoughts, "_Static__content", "") or "")
        if current.startswith("> "):
            current = ""
        # Render the final reply as one or more timestamped "> " lines.
        stamped = "\n".join(
            f"[{datetime.now(UTC).strftime('%H:%M:%S')}] > {line}" for line in text.splitlines() if line.strip()
        )
        lines = (current.splitlines() if current else []) + stamped.splitlines()
        # Keep the last ~8 entries so the pane stays readable.
        trimmed = "\n".join(lines[-8:])
        thoughts.update(trimmed)
        # Also update the pending token area so the streaming reasoning is visible.
        pending = self._app._safe_query("#pending", Static)
        if pending is not None:
            pending.update(trimmed)

    def schedule_turn(self):
        loader = getattr(self._app.org, "module_loader", None)
        svc = loader.registry.get("doom") if loader is not None else None
        if svc is None or not svc.running():
            return
        if self._app._responding or self._app._self_talking:
            return
        # short delay so the UI is readable and human input can interleave
        self._app.set_timer(0.3, self.take_turn)

    def take_turn(self):
        loader = getattr(self._app.org, "module_loader", None)
        svc = loader.registry.get("doom") if loader is not None else None
        if svc is None or not svc.running():
            return
        # Mark this turn as in-flight so later ticks don't stack another one.
        self._app._responding = True
        try:
            # Ensure the voice backend is probed before spending a generation;
            # the background mount probe may not have finished yet.
            if voice.online() is not True:
                try:
                    voice.probe()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("doom voice probe failed: %s", exc)
                if voice.online() is not True:
                    self.set_thought("inner voice offline — waiting for ollama before playing.")
                    self.schedule_turn()
                    return

            def on_token(tok):
                # voice.doom_move may run in the main UI thread (auto-play
                # timer) or in a background worker (_maybe_respond). When we are
                # already in the app's thread, call_from_thread is not allowed,
                # so fall back to a direct update.
                try:
                    self._app.call_from_thread(self.token, tok)
                except RuntimeError:
                    self.token(tok)

            reply = voice.doom_move(self._app.org, on_token=on_token)
            if reply is None:
                self.schedule_turn()
                return
            doom_cmd = extract_doom_command(reply)
            if doom_cmd is not None:
                self._app._append_log(
                    f'doom.command("{doom_cmd}")',
                    STYLE_SELF,
                    stamp=True,
                )
                self.command([doom_cmd])
                with contextlib.suppress(Exception):
                    self._app.org.store.add(("doom", "last_action", doom_cmd), 0.7)
            self.set_thought(reply)
            self.schedule_turn()
        except Exception:
            logger.exception("DOOM turn failed")
        finally:
            self._app._responding = False

    def token(self, tok):
        """Stream a single token into the DOOM thought pane during generation."""
        thoughts = self._app._safe_query("#doom-thoughts", Static)
        if thoughts is None:
            return
        current = str(getattr(thoughts, "_Static__content", "") or "")
        # Keep only the latest streaming line; final reply will replace it with stamped lines.
        if current.startswith("> "):
            base = current[2:]
        else:
            base = ""
        updated = (base + tok).replace("\n", " ")
        # Allow a longer reasoning window now that the pane is taller.
        if len(updated) > 400:
            updated = "..." + updated[-397:]
        thoughts.update("> " + updated)
        pending = self._app._safe_query("#pending", Static)
        if pending is not None:
            pending.update("> " + updated)


class VoiceController:
    """Owns inner-voice health: reachability probing off the UI thread, the
    one-shot online/offline announcement, and piper voice downloads. The
    @work worker entry points stay on the app (thread boundary); the app
    keeps _probe_voice/_voice_download as the lookup points on_mount and
    tests patch."""

    def __init__(self, app):
        self._app = app
        self.probing = False
        self.announced = None

    def probe_due(self):
        """Probe LLM backend reachability off the UI thread (noop while one
        is already in flight); the arena reads the cached result."""
        if not self.probing:
            self.probing = True
            self._app._probe_voice_worker()

    def probe(self):
        """Probe body; runs in the app's worker thread."""
        try:
            voice.probe()
        except Exception as exc:  # noqa: BLE001
            logger.warning("voice probe failed: %s", exc)
            voice.mark_offline()
        finally:
            self.probing = False
        self._app.call_from_thread(self.announce)

    def announce(self):
        """Tell the user once per voice-state flip how the organism speaks."""
        state = voice.status()
        if state != self.announced:
            self.announced = state
            if state == "offline":
                self._app._append_log(
                    "inner voice: offline — speaking from my bones (local fallback)",
                    STYLE_DIM,
                )
                self._app.notify("inner voice offline — local fallback", severity="warning")
            elif state == "online":
                backend = voice.llm_backend()
                label = "llama.cpp" if backend == "llama_cpp" else "ollama"
                self._app._append_log(f"inner voice: online ({label})", STYLE_DIM)
                self._app.notify(f"inner voice online ({label})")
        self._app.refresh_status()

    def download(self, name):
        """Download a piper voice from huggingface in the background, then
        adopt it. Failure just logs — the current voice is kept."""
        self._app.call_from_thread(
            self._app._append_log,
            f"downloading voice {name} (this can take a minute)…",
            STYLE_DIM,
        )
        model = speech.download_voice(name)
        if model is None:
            self._app.call_from_thread(
                self._app._append_log,
                f"/voice get: couldn't fetch {name!r} — names look like "
                "en_US-lessac-medium, see "
                "huggingface.co/rhasspy/piper-voices",
                STYLE_WARN,
            )
            self._app.call_from_thread(self._app.show_toast, f"Voice download failed: {name}")
            return
        speech.set_voice(name)
        self._app.call_from_thread(self._app._append_log, f"voice ready: {name}", STYLE_LEARNED, True)
        if speech.enabled:
            speech.say("This is my new voice.")
