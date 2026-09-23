"""The organism core: Organism state, drives, attention, goals and the
BeliefStore that persists beliefs/state.json/genome per organism directory.
The TUI (tui.py) renders this; the arena (arena.py) debates it."""

import contextlib
import json
import logging
import random
import re
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any, ClassVar

import scallopy

from replicanta import config as project_config
from replicanta import extensions, goals, learning, mud, sentiment, telemetry
from replicanta import memory as memory_module
from replicanta.fileutil import atomic_write_text
from replicanta.gitstate import CONDITION_TEXT as GIT_CONDITION_TEXT
from replicanta.gitstate import GitProbe
from replicanta.hooks import HookEngine, scripts_dir_for
from replicanta.lua_host import LuaHost
from replicanta.probe import SystemProbe
from replicanta.skills import SkillStore
from replicanta.threads import ThreadPool, derive_in_thread, make_self_question_thread

logger = logging.getLogger(__name__)

BEL = "bel"
PROVENANCE = "minmaxprob"
CONTRADICTION_THRESHOLD = 0.5
VALID_VALUE_RE = re.compile(r"^[a-z_]+$")

# Objects from the toy object/color seed world. Purged on load: the
# organism's beliefs now come from the real host machine via SystemProbe.
LEGACY_OBJECTS = {"apple", "ball", "milk", "water"}

# How many chat lines (user + organism) are remembered across restarts.
CHAT_LOG_LIMIT = 24

# How many notable episodes (birth, dreams, lessons, harsh moments...) the
# organism carries with it. Injected into the narration prompt so the inner
# voice has continuity instead of starting from zero every time.
MEMORY_LIMIT = 50


def _loaded_belief_ok(parts):
    """Re-validate a [obj, attr, val, conf] entry from state.json."""
    return (
        isinstance(parts, (list, tuple))
        and len(parts) == 4
        and all(isinstance(s, str) and VALID_VALUE_RE.match(s) for s in parts[:3])
        and isinstance(parts[3], (int, float))
        and not isinstance(parts[3], bool)
    )


def _loaded_rule_ok(text):
    """Rule text is rendered as ``rel {text}`` in the genome; a rule must
    never span lines, which is what would let it inject new statements."""
    return isinstance(text, str) and "\n" not in text and "\r" not in text


def _loaded_pair_ok(pair):
    """Re-validate a two-item collection entry (attention pair, chat line)
    from state.json: an iterable of exactly two strings."""
    return isinstance(pair, (list, tuple)) and len(pair) == 2 and all(isinstance(s, str) for s in pair)


def _loaded_memory_ok(m):
    """A memory episode must survive narration (which indexes m['cycle']
    and m['text']) and ranking: a dict with a non-empty text and an int
    cycle stamp."""
    return (
        isinstance(m, dict)
        and isinstance(m.get("text"), str)
        and bool(m["text"].strip())
        and isinstance(m.get("cycle"), int)
        and not isinstance(m["cycle"], bool)
    )


def _loaded_goal_ok(g):
    """A goal must survive active_goal()/_goals_tick/mind_view, which index
    text, done_cycle, created_cycle and marker: a dict with a non-empty
    text, int cycle stamps (done_cycle may be None) and a numeric marker.
    Entries missing any of these are dropped like other filtered state."""
    if not isinstance(g, dict):
        return False
    if not isinstance(g.get("text"), str) or not g["text"].strip():
        return False
    for key in ("created_cycle", "marker"):
        value = g.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
    if "done_cycle" not in g:
        return False
    done = g["done_cycle"]
    return done is None or (isinstance(done, (int, float)) and not isinstance(done, bool))


def _as_list(value):
    """Coerce a loaded JSON field to a list; anything else reads as empty;
    a corrupt-but-parseable state.json must not crash boot."""
    return value if isinstance(value, list) else []


class BeliefStore:
    """In-memory belief dict + archived beliefs + chaos + cycle, persisted to
    state.json. `organism.scl` is rendered from this on every save."""

    def __init__(self, dir_path):
        self.dir_path = dir_path
        self.scl_path = dir_path / "organism.scl"
        self.state_path = dir_path / "state.json"
        # Workers (@work threads in the TUI, the web scheduler, group chat)
        # mutate the store concurrently with the tick thread that serializes
        # it in save(). RLock (not Lock) because mutators legitimately nest
        # (add() -> remember()) on one thread. NEVER hold this lock while
        # firing Lua hooks (on_utterance -> hooks.fire takes the Lua host
        # lock; the host can call back into the store -> ABBA deadlock) or
        # while rebuilding the Scallop Mind.
        self._lock = threading.RLock()
        # Bumped by every mutation that can change what narration's snapshot
        # renders; the snapshot cache keys on it so confidence-only updates
        # and same-length chat/memory edits are never served stale.
        self.version = 0
        # Set when a persisted file had to be quarantined at load(); the web
        # front-end renders it as a one-time warning banner.
        self.load_error = None
        # Back-reference the owning Lifecycle so save() can persist its state
        # (organism.py wires it; standalone stores in tests leave it None).
        self.lifecycle = None
        # Lifecycle/wall-clock persistence (see Organism._restore_lifecycle):
        # state + transition time of the lifecycle, and the wall time of the
        # last save, so a restart can advance the body by the time lived away.
        self.lifecycle_state = None
        self.lifecycle_started = None
        self.last_wall = None
        # Self-stated intentions harvested from the entity's own replies
        # (learning.assimilate_own_reply) — goal candidates the prompt can
        # mention and _goals_tick can promote when one keeps recurring.
        self.self_goal_candidates = []
        # Said-vs-held flags: the entity's reply denied something it holds.
        self.said_vs_held = []
        self.beliefs_map: dict[tuple[str, str, str], float] = {}
        self.archived_map: dict[tuple[str, str, str], float] = {}
        self._by_obj_attr: dict[tuple[str, str], dict[str, float]] = {}
        self._derived_cache: dict[str, Any] | None = None
        self.chaos = 0.5
        self.stress = 0.05
        self.arousal = 0.15  # activation/energy; low at birth
        self.coherence = 0.5  # grounded coherence (see MentalState)
        self.incoherence = 0.2  # chaos/stress-driven incoherence
        self.insane = False  # extreme stress + incoherence
        self.fade_streak = 0  # consecutive transitions at critical stress
        self.cycle = 0
        self.rule_counter = 0
        self.rules = []  # list of (text, depth)
        self.attention = set()  # (attr, val) pairs in the window
        self.chat_log = []  # list of [role, text], capped by CHAT_LOG_LIMIT
        self.memory = []  # episodes: {"cycle", "kind", "text", "importance", "recall"}
        self.threads = {}  # id -> CognitiveThread
        self.thread_results = deque(maxlen=20)  # harvested thread summaries
        self.goals = []  # {"text","created_cycle","done_cycle","marker"}
        self.last_goal_cycle = 0
        self.last_diary_cycle = 0
        self.last_reflect_cycle = 0
        self.activity = {}  # neurosymbolic activity counters (activity.py)
        self.fatigue = 0.0  # sleep debt; rises while wake, resets on sleep
        self.surprise_this_tick = False
        self.on_adverse = None  # callback(amount) fired on contradiction
        self.on_utterance = None  # callback(role, text) fired on chat lines
        self.dirty = False  # any state changed since last save()
        self.genome_dirty = False  # beliefs/rules changed -> .scl needs rewrite
        self.auto_apply_patches = False  # organism self-patches require approval
        # Whether loaded modules may actuate the entity (move its body, run
        # games on its behalf). Persisted next to auto_apply_patches; modules
        # read it through their capability bridges.
        self.entity_actuation = True

    def _bump_version(self):
        """Mark the rendered mind-state changed (narration cache invalidation)."""
        self.version += 1

    def _index_belief(self, belief, conf):
        """Update the (obj, attr) index for quick contradiction lookup."""
        obj, attr, val = belief
        self._by_obj_attr.setdefault((obj, attr), {})[val] = conf

    def _unindex_belief(self, belief):
        obj, attr, val = belief
        key = (obj, attr)
        if key in self._by_obj_attr and val in self._by_obj_attr[key]:
            del self._by_obj_attr[key][val]
            if not self._by_obj_attr[key]:
                del self._by_obj_attr[key]

    def _invalidate_derived(self):
        self._derived_cache = None
        self._bump_version()

    # -- belief operations -------------------------------------------------
    def note_activity(self, key, n=1):
        """Increment one neurosymbolic-activity counter (see activity.py
        for the key taxonomy). Persisted with state.json."""
        with self._lock:
            self.activity[key] = self.activity.get(key, 0) + n
            self.dirty = True
            self._bump_version()

    def _record_surprise(self, old_belief, new_belief):
        """A held belief was contradicted and archived. Keep the last ten
        surprises in activity["surprises"] for the voice prompt."""
        with self._lock:
            surprises = self.activity.setdefault("surprises", [])
            surprises.append(
                {
                    "cycle": self.cycle,
                    "old": learning.describe(old_belief),
                    "new": learning.describe(new_belief),
                }
            )
            while len(surprises) > 10:
                surprises.pop(0)
            self.surprise_this_tick = True
            self.dirty = True
            self._bump_version()

    def _derive_from_beliefs(self, rule, head_relation):
        """Run a transient Scallop rule against the live in-memory belief map.
        Does not require a committed genome, so derived() reflects the
        current organism state even before flush(). The belief facts are
        snapshotted under the store lock; the Scallop context is created,
        run, and dropped on the calling thread without holding the lock."""
        with self._lock:
            facts = [(conf, (obj, attr, val)) for (obj, attr, val), conf in self.beliefs_map.items()]
        ctx = scallopy.ScallopContext(provenance=PROVENANCE)
        ctx.add_relation(BEL, (str, str, str))
        ctx.add_facts(BEL, facts)
        ctx.add_rule(rule)
        ctx.run()
        return [(float(tag), tuple(tup)) for (tag, tup) in ctx.relation(head_relation)]

    def derived(self):
        """Scallop-derived conditions visible to prompts and behavior code.
        Returns dict with 'needs_user' and 'contradictions'."""
        with self._lock:
            cached = self._derived_cache
        if cached is not None:
            return cached
        contradicts_rule = "contradicts(o, a) = bel(o, a, v1) and bel(o, a, v2) and v1 != v2"
        needs_user_rule = 'needs_user(o) = bel(o, "is_a", "organism") and not bel("user", _, _)'
        contradictions = [
            {"obj": obj, "attr": attr, "tag": float(tag)}
            for tag, (obj, attr) in self._derive_from_beliefs(contradicts_rule, "contradicts")
            if tag >= CONTRADICTION_THRESHOLD
        ]
        needs_user = any(
            tag >= CONTRADICTION_THRESHOLD for tag, _ in self._derive_from_beliefs(needs_user_rule, "needs_user")
        )
        derived = {
            "needs_user": needs_user,
            "contradictions": contradictions,
        }
        with self._lock:
            self._derived_cache = derived
        return derived

    def _note_scallop_contradictions(self):
        """Log reasoner-detected contradictions as activity and memory."""
        for c in self.derived()["contradictions"]:
            tag = c["tag"]
            obj = c["obj"]
            attr = c["attr"]
            if tag >= CONTRADICTION_THRESHOLD:
                self.note_activity("scallop_contradiction")
                memory_text = f"Scallop saw tension: {obj}:{attr} holds two values"
                with self._lock:
                    recent = [m.get("text") for m in self.memory[-20:]]
                if memory_text not in recent:
                    self.remember("surprise", memory_text)

    def add(self, belief, conf):
        obj, attr, val = belief
        if not VALID_VALUE_RE.match(obj) or not VALID_VALUE_RE.match(attr) or not VALID_VALUE_RE.match(val):
            raise ValueError(f"invalid belief value in {belief}")
        conf = float(conf)
        key = (obj, attr, val)
        contradiction_seen = False
        adverse = 0.0
        with self._lock:
            for (o, a, v), c in list(self.beliefs_map.items()):
                if (
                    (o, a) == (obj, attr)
                    and v != val
                    and c >= CONTRADICTION_THRESHOLD
                    and conf >= CONTRADICTION_THRESHOLD
                ):
                    contradiction_seen = True
                    adverse = 0.03
                    if conf > c:
                        self.archived_map[(o, a, v)] = c
                        del self.beliefs_map[(o, a, v)]
                        self._unindex_belief((o, a, v))
                        self.note_activity("beliefs_archived")
                        self._record_surprise((o, a, v), belief)
                    else:
                        self.archived_map[key] = conf
                        self.note_activity("beliefs_archived")
                        self._record_surprise(belief, (o, a, v))
                    self.dirty = True
                    self.genome_dirty = True
                    self._invalidate_derived()
                    break
        # Outside the lock: on_adverse may run user code.
        if adverse and self.on_adverse is not None:
            self.on_adverse(adverse)
        if contradiction_seen:
            return
        # The reasoner contradiction note reflects the map as it was BEFORE
        # this insertion (the insert below invalidates the derived cache) —
        # the historical ordering, kept deliberately.
        self._note_scallop_contradictions()
        with self._lock:
            if key in self.beliefs_map:
                if conf > self.beliefs_map[key]:
                    self.beliefs_map[key] = conf
                    self._index_belief(key, conf)
                    self.note_activity("beliefs_strengthened")
                    self.dirty = True
                    self.genome_dirty = True
                    self._invalidate_derived()
            else:
                self.beliefs_map[key] = conf
                self._index_belief(key, conf)
                self.note_activity("beliefs_new")
                self.dirty = True
                self.genome_dirty = True
                self._invalidate_derived()

    def conf(self, belief):
        """Confidence for ``belief``, or None when it is not held."""
        return self.beliefs_map.get(belief)

    def observe(self, belief, conf):
        """Replace the current reading for (obj, attr): a fresh perception
        supersedes the old one without triggering the contradiction/archive
        path (which is reserved for conflicting internal derivations). Any
        coexisting value for the same (obj, attr) is dropped, including
        sub-threshold leftovers that add()'s contradiction gate lets sit
        alongside the held value."""
        obj, attr, val = belief
        if not VALID_VALUE_RE.match(obj) or not VALID_VALUE_RE.match(attr) or not VALID_VALUE_RE.match(val):
            raise ValueError(f"invalid belief value in {belief}")
        conf = float(conf)
        key = (obj, attr, val)
        changed = False
        with self._lock:
            for o, a, v in list(self.beliefs_map):
                if (o, a) == (obj, attr):
                    if v == val and self.beliefs_map[(o, a, v)] == conf:
                        continue  # unchanged reading: keep the existing entry
                    del self.beliefs_map[(o, a, v)]
                    self._unindex_belief((o, a, v))
                    changed = True
            if changed or key not in self.beliefs_map:
                self.beliefs_map[key] = conf
                self._index_belief(key, conf)
                self.dirty = True
                self.genome_dirty = True
                self._invalidate_derived()

    def beliefs(self):
        """Return a shallow copy of the live belief map."""
        with self._lock:
            return dict(self.beliefs_map)

    def belief_value(self, obj, attr, default=None):
        """Value of the first (obj, attr) belief, else default."""
        return next((v for (bo, ba, v) in self.beliefs() if (bo, ba) == (obj, attr)), default)

    def count_beliefs(self, obj):
        """Count beliefs whose object matches `obj`."""
        return sum(1 for (o, _a, _v) in self.beliefs() if o == obj)

    def archived(self):
        """Return a shallow copy of the archived belief map."""
        with self._lock:
            return dict(self.archived_map)

    # -- chat memory -------------------------------------------------------
    def record_chat(self, role, text):
        """Append one chat line (role: "user" | "org"), trimmed to
        CHAT_LOG_LIMIT. Persisted with state.json so the organism
        remembers the conversation across restarts. The on_utterance hook
        fires AFTER the lock is released: hooks take the Lua host lock,
        which can call back into the store (ABBA deadlock otherwise)."""
        text = text.strip()
        if not text:
            return
        fire = None
        with self._lock:
            self.chat_log.append([role, text])
            if len(self.chat_log) > CHAT_LOG_LIMIT:
                del self.chat_log[: len(self.chat_log) - CHAT_LOG_LIMIT]
            self.dirty = True
            self._bump_version()
            if role == "org" and self.on_utterance is not None:
                fire = self.on_utterance
        if fire is not None:
            fire(role, text)

    # -- rules -------------------------------------------------------------
    def commit_rule(self, text, depth):
        """Commit a derived rule to the genome (marks it for rewriting)."""
        with self._lock:
            self.rules.append((text, depth))
            self.note_activity("rules_committed")
            self.dirty = True
            self.genome_dirty = True
            self._bump_version()

    # -- goals ---------------------------------------------------------------
    def add_goal(self, text, marker=0, strategy=None):
        """Form a new intention: one active goal at a time, cycle-stamped.
        `marker` records the progress baseline (e.g. user-fact count at
        formation) so the engine can tell when the goal is achieved.

        Refuses (returns None) while another goal is still active, so the
        queue never holds two unfinished fronts; completed-goal history is
        left intact."""
        with self._lock:
            if self.active_goal() is not None:
                return None
            self.goals.append(
                {
                    "text": text,
                    "created_cycle": self.cycle,
                    "done_cycle": None,
                    "marker": marker,
                    "strategy": strategy,
                }
            )
            self.dirty = True
            self._bump_version()
            return self.goals[-1]

    def active_goal(self):
        """Return the first unfinished goal, or None."""
        with self._lock:
            return next((g for g in self.goals if g["done_cycle"] is None), None)

    def complete_active_goal(self):
        """Mark the active goal done and return it (or None)."""
        with self._lock:
            goal = self.active_goal()
            if goal is not None:
                goal["done_cycle"] = self.cycle
                self.dirty = True
                self._bump_version()
            return goal

    # -- episodic memory ----------------------------------------------------
    def remember(self, kind, text):
        """Record one notable episode (cycle-stamped), capped at
        MEMORY_LIMIT with oldest-first eviction. `kind` is a free-form
        tag; MUD events are recorded with kind "mud" by the TUI."""
        with self._lock:
            entry = {"cycle": self.cycle, "kind": kind, "text": text}
            memory_module.attach_importance(entry, current_cycle=self.cycle)
            self.memory.append(entry)
            if len(self.memory) > MEMORY_LIMIT:
                del self.memory[: len(self.memory) - MEMORY_LIMIT]
            self.dirty = True
            self._bump_version()
            return entry

    # -- self-statement candidates (speech -> state loop) --------------------
    def add_self_goal_candidate(self, text):
        """Record an intention the entity stated in its own reply. Repeat
        statements of the same intention raise its count; kept small and
        persisted so _goals_tick can promote one that keeps recurring."""
        text = " ".join(str(text).split())
        if len(text) > 80:
            # cut at a word boundary — a mid-word slice reads as gibberish
            # in the prompt ("...asking for more details. What do")
            text = text[:80].rsplit(" ", 1)[0].rstrip(",;:.!?") + "…"
        if len(text) < 3:
            return None
        with self._lock:
            for candidate in self.self_goal_candidates:
                if candidate["text"] == text:
                    candidate["count"] = candidate.get("count", 1) + 1
                    candidate["cycle"] = self.cycle
                    self.dirty = True
                    self._bump_version()
                    return candidate
            entry = {"text": text, "cycle": self.cycle, "count": 1}
            self.self_goal_candidates.append(entry)
            if len(self.self_goal_candidates) > 5:
                del self.self_goal_candidates[: len(self.self_goal_candidates) - 5]
            self.dirty = True
            self._bump_version()
            return entry

    def pop_self_goal_candidate(self):
        """Remove and return the strongest self-stated goal candidate (highest
        count, freshest cycle), or None when the well of stated intentions
        is dry."""
        with self._lock:
            if not self.self_goal_candidates:
                return None
            best = max(self.self_goal_candidates, key=lambda c: (c.get("count", 1), c.get("cycle", 0)))
            self.self_goal_candidates.remove(best)
            self.dirty = True
            self._bump_version()
            return best

    def note_said_vs_held(self, said, held):
        """Flag that the entity's own reply denied something it holds.
        Surfaced in the prompt so the next utterance can square it."""
        with self._lock:
            self.said_vs_held.append({"cycle": self.cycle, "said": str(said)[:80], "held": str(held)[:80]})
            while len(self.said_vs_held) > 5:
                self.said_vs_held.pop(0)
            self.dirty = True
            self._bump_version()

    # -- cognitive threads ---------------------------------------------------
    def queue_thread(self, thread):
        """Store a thread and mark state dirty."""
        with self._lock:
            self.threads[thread.id] = thread
            thread.status = "pending"
            self.dirty = True
        return thread.id

    def start_thread(self, thread_id):
        """Mark a thread as running."""
        with self._lock:
            thread = self.threads.get(thread_id)
            if thread is not None:
                thread.status = "running"
                self.dirty = True

    def finish_thread(self, thread_id, result=None, error=None):
        """Finalize a thread, archive its result, and remove it from active."""
        with self._lock:
            thread = self.threads.get(thread_id)
            if thread is None:
                return
            thread.status = "failed" if error else "done"
            thread.result = result
            thread.error = error
            self.thread_results.append(
                {
                    "id": thread.id,
                    "kind": thread.kind,
                    "cycle": thread.created_cycle,
                    "result": result,
                    "error": error,
                }
            )
            del self.threads[thread_id]
            self.dirty = True

    # -- MUD session --------------------------------------------------------
    @property
    def mud_state_path(self):
        return self.dir_path / "artifacts" / "mud_state.json"

    def save_mud_session(self, session):
        """Persist a mud.MudSession to artifacts/mud_state.json (atomic)."""
        self.mud_state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.mud_state_path, json.dumps(session.to_json(), indent=2))

    def load_mud_session(self):
        """Load the persisted mud.MudSession, or None when the file is
        missing or corrupt."""
        if not self.mud_state_path.exists():
            return None
        try:
            return mud.MudSession.from_json(json.loads(self.mud_state_path.read_text()))
        except Exception:  # noqa: BLE001 — a corrupt save must never kill the organism
            return None

    # -- rendering + persistence -------------------------------------------
    def render_scl(self):
        with self._lock:
            lines = ["// Scallop Organism — genome (generated by the runtime)"]
            for (obj, attr, val), conf in sorted(self.beliefs_map.items()):
                lines.append(f'rel {conf}::{BEL}("{obj}", "{attr}", "{val}")')
            for text, _depth in self.rules:
                lines.append(f"rel {text}")
            return "\n".join(lines) + "\n"

    def save(self):
        """Serialize the live state to organism.scl + state.json.

        Everything is snapshotted under the store lock (json.dumps over live
        references is what raced worker mutations before); the file writes
        happen after the lock is released so mutators never block on I/O.
        Mutations landing between snapshot and write set dirty again, so the
        next save picks them up — nothing is lost by writing a stale copy."""
        self.dir_path.mkdir(parents=True, exist_ok=True)
        with self._lock:
            genome = None
            if self.genome_dirty or not self.scl_path.exists():
                genome = self.render_scl()
                self.genome_dirty = False
            now = time.time()
            lifecycle = self.lifecycle
            state = {
                "chaos": self.chaos,
                "stress": self.stress,
                "arousal": self.arousal,
                "coherence": self.coherence,
                "incoherence": self.incoherence,
                "insane": self.insane,
                "fade_streak": self.fade_streak,
                "cycle": self.cycle,
                "rule_counter": self.rule_counter,
                "rules": list(self.rules),
                "beliefs": [list(k) + [v] for k, v in self.beliefs_map.items()],
                "archived": [list(k) + [v] for k, v in self.archived_map.items()],
                "attention": [list(p) for p in self.attention],
                "chat": list(self.chat_log),
                "memory": list(self.memory),
                "thread_results": list(self.thread_results),
                "goals": [dict(g) for g in self.goals],
                "last_goal_cycle": self.last_goal_cycle,
                "last_diary_cycle": self.last_diary_cycle,
                "last_reflect_cycle": self.last_reflect_cycle,
                "activity": dict(self.activity),
                "fatigue": self.fatigue,
                "auto_apply_patches": self.auto_apply_patches,
                "entity_actuation": self.entity_actuation,
                "self_goal_candidates": list(self.self_goal_candidates),
                "said_vs_held": list(self.said_vs_held),
                # lifecycle + wall-clock persistence: restored on the next boot
                # so organisms live between runs (see Organism._restore_lifecycle)
                "lifecycle_state": lifecycle.state if lifecycle is not None else self.lifecycle_state,
                "lifecycle_started": (lifecycle.state_started if lifecycle is not None else self.lifecycle_started),
                "last_wall": now,
            }
            payload = json.dumps(state, indent=2)
            self.last_wall = now
            self.dirty = False
        if genome is not None:
            atomic_write_text(self.scl_path, genome)
        atomic_write_text(self.state_path, payload)

    @staticmethod
    def _stamp_for(path):
        """A collision-free quarantine suffix: second-precision timestamp,
        extended with microseconds when the same file fails twice in a second."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        target = path.with_name(f"{path.name}.corrupt-{stamp}")
        counter = 0
        while target.exists():
            counter += 1
            target = path.with_name(f"{path.name}.corrupt-{stamp}-{counter}")
        return target

    def quarantine_file(self, path, description):
        """Rename a damaged persisted file aside (never overwrite it) and
        remember the problem so the front-end can surface a warning. The
        organism boots factory defaults instead of silently forgetting."""
        try:
            target = self._stamp_for(path)
            path.rename(target)
        except OSError as exc:
            logger.warning("could not quarantine %s: %s", path, exc)
            self.load_error = f"{path.name} was corrupt ({description}) and could not be preserved"
            return None
        self.load_error = f"{path.name} was corrupt ({description}); preserved as {target.name}"
        logger.warning("%s; started from factory defaults", self.load_error)
        return target

    def load(self):
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, ValueError) as exc:
            # Corruption must not mean silent amnesia: quarantine the bad
            # file (boot defaults stay in place) and expose the problem.
            self.quarantine_file(self.state_path, str(exc))
            return
        if not isinstance(state, dict):
            self.quarantine_file(self.state_path, "top-level JSON value is not an object")
            return  # valid JSON, wrong shape: keep fresh defaults
        with self._lock:
            self.chaos = state.get("chaos", 0.5)
            self.stress = state.get("stress", 0.05)
            self.arousal = state.get("arousal", 0.15)
            # Old state.json files carry the rationality/irrationality names; read
            # them as a fallback so existing organisms migrate on first load.
            self.coherence = state.get("coherence", state.get("rationality", 0.5))
            self.incoherence = state.get("incoherence", state.get("irrationality", 0.2))
            self.insane = state.get("insane", False)
            self.fade_streak = state.get("fade_streak", 0)
            self.fatigue = state.get("fatigue", 0.0)
            self.cycle = state.get("cycle", 0)
            self.rule_counter = state.get("rule_counter", 0)
            # state.json is app-owned (0600) but load() must re-validate anyway:
            # beliefs/rules are rendered verbatim into the .scl genome, so a
            # tampered state file would otherwise inject Scallop relations.
            self.rules = [
                (r[0], int(r[1]))
                for r in _as_list(state.get("rules"))
                if isinstance(r, (list, tuple))
                and len(r) == 2
                and _loaded_rule_ok(r[0])
                and isinstance(r[1], (int, float))
                and not isinstance(r[1], bool)
            ]
            self.beliefs_map = {
                (b[0], b[1], b[2]): float(b[3]) for b in _as_list(state.get("beliefs")) if _loaded_belief_ok(b)
            }
            self.archived_map = {
                (b[0], b[1], b[2]): float(b[3]) for b in _as_list(state.get("archived")) if _loaded_belief_ok(b)
            }
            self.attention = {tuple(p) for p in _as_list(state.get("attention")) if _loaded_pair_ok(p)}
            self.chat_log = [list(c) for c in _as_list(state.get("chat")) if _loaded_pair_ok(c)]
            self.memory = [
                memory_module.attach_importance(dict(m), current_cycle=self.cycle)
                for m in _as_list(state.get("memory"))
                if _loaded_memory_ok(m)
            ]
            self.thread_results = deque(_as_list(state.get("thread_results")), maxlen=20)
            self.goals = [dict(g) for g in _as_list(state.get("goals")) if _loaded_goal_ok(g)]
            self.last_goal_cycle = state.get("last_goal_cycle", 0)
            self.last_diary_cycle = state.get("last_diary_cycle", 0)
            self.last_reflect_cycle = state.get("last_reflect_cycle", 0)
            self.auto_apply_patches = state.get("auto_apply_patches", False)
            self.entity_actuation = state.get("entity_actuation", True)
            self.self_goal_candidates = [
                dict(c)
                for c in _as_list(state.get("self_goal_candidates"))
                if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip()
            ][-5:]
            self.said_vs_held = [
                dict(f)
                for f in _as_list(state.get("said_vs_held"))
                if isinstance(f, dict) and isinstance(f.get("said"), str) and isinstance(f.get("held"), str)
            ][-5:]
            self.lifecycle_state = state.get("lifecycle_state")
            self.lifecycle_started = state.get("lifecycle_started")
            self.last_wall = state.get("last_wall")
            self.activity = {}
            activity = state.get("activity", {})
            if isinstance(activity, dict):
                for k, v in activity.items():
                    if isinstance(v, (list, dict)):
                        self.activity[k] = v
                    else:
                        try:
                            self.activity[k] = int(v)
                        except (TypeError, ValueError):
                            continue  # an uncountable value drops with the counter
            self.dirty = False
            self._bump_version()


# Scallop contexts are thread-affine: they must be created and dropped on the
# same thread. Minds form reference cycles (the store's on_utterance callback
# closes over the organism), so the cyclic GC may collect a Mind on any
# thread. Contexts that would be dropped on the wrong thread are parked here,
# keyed by their owning Thread, and released by the next Mind operation or
# close() running on that thread. Thread idents are recycled after a thread
# exits, so ownership is keyed by the Thread object itself, never by ident. A
# context parked for a thread that has exited leaks silently — there is no
# safe thread left to drop it on.
_ORPHAN_CONTEXTS: dict[threading.Thread, list] = {}


def _release_context(ref, owner_thread):
    """Release the context held by the one-element list ``ref`` on the thread
    that created it.

    ``ref`` must hold the sole remaining reference (callers detach every
    other one first) so the drop happens inside this function. On the owning
    thread the context drops before this returns; from any other thread it is
    parked until the owner drains it (see ``Mind.close``).
    """
    if threading.current_thread() is owner_thread:
        ref.clear()
    else:
        _ORPHAN_CONTEXTS.setdefault(owner_thread, []).append(ref[0])
        ref.clear()


def _drain_orphan_contexts(owner_thread):
    """Drop contexts parked for ``owner_thread``. Caller must be on it."""
    parked = _ORPHAN_CONTEXTS.pop(owner_thread, None)
    if parked:
        parked.clear()


class Mind:
    """The Scallop program. Rebuilds the context from the .scl genome, runs it,
    and exposes belief facts with their minmaxprob confidences.

    Scallop contexts are thread-affine. ``Mind`` keeps the context created by
    the owning thread in ``self.ctx`` and rebuilds a fresh context on any
    other thread so reasoning stays safe under ``ThreadingHTTPServer``.
    """

    def __init__(self, scl_path):
        """Build a Mind for the .scl genome at ``scl_path``."""
        self.scl_path = scl_path
        self.ctx = None
        self._owner_thread = threading.current_thread()

    def _thread_context(self):
        """Return a ScallopContext usable on the current thread.

        On the owning thread, reuse ``self.ctx`` (rebuilding if needed). On
        any other thread, build a fresh context from the .scl file without
        touching ``self.ctx``. Both front-ends reason on the owning thread
        (the TUI ticks on one thread; the web server is single-threaded per
        organism), so the per-call import cost off-thread is a safety
        fallback, not a hot path.
        """
        me = threading.current_thread()
        if me is self._owner_thread:
            if self.ctx is None:
                self.rebuild()
            _drain_orphan_contexts(me)
            return self.ctx
        ctx = scallopy.ScallopContext(provenance=PROVENANCE)
        if self.scl_path.exists():
            ctx.import_file(str(self.scl_path))
        ctx.run()
        return ctx

    def rebuild(self):
        """Re-import the .scl genome and run the Scallop program.

        The calling thread becomes the context owner (scallopy contexts are
        thread-affine). A previous context owned by another thread is handed
        back to that thread for release rather than dropped here.
        """
        me = threading.current_thread()
        ref, self.ctx = [self.ctx], None
        _release_context(ref, self._owner_thread)
        self.ctx = scallopy.ScallopContext(provenance=PROVENANCE)
        self._owner_thread = me
        if self.scl_path.exists():
            self.ctx.import_file(str(self.scl_path))
        self.ctx.run()
        _drain_orphan_contexts(me)

    def beliefs(self):
        """Return the current genome beliefs as ``(obj, attr, val): conf`` dict."""
        out = {}
        for tag, tup in self._thread_context().relation(BEL):
            out[tuple(tup)] = float(tag)
        return out

    def query_rule(self, rule, head_relation):
        """Run a candidate rule against a fork of the current program without
        committing. Returns list of (tag, tuple)."""
        return self.derive(head_relation, rule)

    def derive(self, head_relation, rule):
        """Run a transient derived rule against a fresh fork and return the
        derived tuples with their minmaxprob tags. Safe for read-only
        inference queries."""
        ctx = scallopy.ScallopContext(provenance=PROVENANCE, fork_from=self._thread_context())
        ctx.add_rule(rule)
        ctx.run()
        return [(float(tag), tuple(tup)) for (tag, tup) in ctx.relation(head_relation)]

    def close(self):
        """Release the context. Safe to call from any thread.

        On the owning thread the context drops immediately, along with any
        contexts parked for this thread; from any other thread the context is
        parked until the owner releases it.
        """
        me = threading.current_thread()
        if me is self._owner_thread:
            self.ctx = None
            _drain_orphan_contexts(me)
        else:
            ref, self.ctx = [self.ctx], None
            _release_context(ref, self._owner_thread)

    def __del__(self):
        # The cyclic GC may collect the Mind on any thread; never let it drop
        # a thread-affine context outside the owning thread.
        try:
            ref, self.ctx = [self.ctx], None
            owner = self._owner_thread
        except AttributeError:
            return  # partially initialised Mind
        with contextlib.suppress(Exception):  # interpreter teardown must stay quiet
            _release_context(ref, owner)


class ChaosKnob:
    """Live-tunable 0..1 randomness constant. High = novel self-questions,
    wild dreams, wandering. Low = conservative consolidation."""

    def __init__(self, value=0.5):
        """Create a clamped 0..1 chaos knob."""
        self.value = max(0.0, min(1.0, float(value)))

    def set(self, value):
        """Clamp and store a new chaos value."""
        self.value = max(0.0, min(1.0, float(value)))

    def roll(self, rng):
        """True with probability = chaos."""
        return rng.random() < self.value


class StressMeter:
    """Tracks the organism's stress (0.0-1.0, baseline 0.05), held in
    `BeliefStore.stress` and persisted with state.json. Adverse experiences
    bump it up; the excess above baseline then decays exponentially —
    slowly while awake (tens of minutes), faster asleep — so an upset stays
    emotionally readable instead of vanishing in seconds. Wakefulness adds
    a small sleep-debt pressure and draining moods push toward their
    (bounded) asymptotes; neither can reach the fade zone alone, only real
    adverse events can. High stress feeds back into the chaos knob via
    `Organism.chaos_effective()`."""

    BASELINE = 0.05
    # Per-second fraction of the excess above baseline that bleeds off while
    # awake (~19-minute half-life); sleep clears it SLEEP_DECAY_MULT times
    # faster (~5-minute half-life).
    WAKE_DECAY_RATE = 0.0006
    SLEEP_DECAY_MULT = 4.0
    # Constant wake pressure (asymptote ≈ 0.55: a long day is taxing but can
    # never fade the organism by itself) and draining-mood pressure, the
    # latter scaled by remaining headroom so a bad mood approaches but never
    # reaches madness or the fade zone without fresh adverse events.
    SLEEP_DEBT_RATE = 0.0003
    NEGATIVE_MOOD_RATE = 0.0004
    NEGATIVE_MOODS: ClassVar[set] = {
        "sad",
        "angry",
        "anxious",
        "afraid",
        "hurt",
        "fraying",
        "unhinged",
        "insane",
    }

    def __init__(self, store):
        """Track stress for ``store``; mutations are written back to the store."""
        self.store = store

    @property
    def value(self):
        """Current stress level (0.0-1.0)."""
        return self.store.stress

    def _clamp(self, value):
        return max(0.0, min(1.0, value))

    def bump(self, amount):
        """Adverse-experience hook: raise stress by amount, clamped."""
        self.store.stress = self._clamp(self.store.stress + amount)

    def tick(self, sleeping, dt=1.0):
        """Advance stress by dt seconds of lived time. The excess above
        baseline decays exponentially (faster asleep); wakefulness adds
        sleep-debt pressure and draining moods push upward, both bounded."""
        excess = max(0.0, self.store.stress - self.BASELINE)
        decay = self.WAKE_DECAY_RATE * (self.SLEEP_DECAY_MULT if sleeping else 1.0)
        stress = self.store.stress - excess * min(1.0, decay * dt)
        if sleeping:
            stress = max(stress, self.BASELINE)
        else:
            stress += self.SLEEP_DEBT_RATE * dt
            if self._negative_mood():
                stress += self.NEGATIVE_MOOD_RATE * max(0.0, 1.0 - stress) * dt
        self.store.stress = self._clamp(stress)

    def _negative_mood(self):
        """True when the organism's current mood is one of the draining ones."""
        mood = self.store.belief_value("self", "mood")
        return mood in self.NEGATIVE_MOODS


class MentalState:
    """Arousal, coherence and incoherence (0-1 each), EMA-smoothed
    every tick and persisted in state.json. Arousal is activation/energy;
    coherence is grounded coherence (fed by the grounding proxy from the
    activity meter, lowered by chaos and stress); incoherence is
    chaos/stress-driven unraveling. When stress is extreme and
    incoherence dominates, the organism is insane: its mood reads
    'insane' and the voice is told it is incoherent. Hysteresis keeps the
    flag from flapping near the thresholds."""

    INSANE_STRESS = 0.75  # extreme stress
    INSANE_IRRATIONALITY = 0.6  # incoherence dominance
    RECOVERY_STRESS = 0.6  # recovery needs stress below this …
    RECOVERY_IRRATIONALITY = 0.45  # … AND incoherence below this …
    RECOVERY_SECONDS = 300.0  # … sustained this long (~5 min of lived time)
    SMOOTHING = 0.25  # EMA share per tick-second
    WAKE_FATIGUE_RATE = 0.02  # fatigue per second while awake
    SLEEP_RECOVERY_RATE = 0.08  # fatigue recovered per second while asleep
    # circadian mode: sleep debt accrues over waking hours, not seconds, so
    # the lifecycle can hold a real day schedule; recovery is sized for a
    # ~45-min afternoon doze to erase most of a day's tiredness
    CIRCADIAN_WAKE_FATIGUE_RATE = 0.75 / (10 * 3600)  # nap-worthy after ~10h awake
    CIRCADIAN_SLEEP_RECOVERY_RATE = 0.65 / (45 * 60)  # 0.75 -> 0.10 in 45 min
    INSANE_MEMORY_DECAY = 0.002  # importance per second lost from memories while insane

    def __init__(self, store, circadian=False):
        """MentalState smooths arousal/coherence/incoherence and decides the
        insane flag with hysteresis. ``circadian`` swaps fatigue accrual to
        the day-scale rates that the circadian lifecycle schedules against."""
        self.store = store
        self._wake_fatigue_rate = self.CIRCADIAN_WAKE_FATIGUE_RATE if circadian else self.WAKE_FATIGUE_RATE
        self._sleep_recovery_rate = self.CIRCADIAN_SLEEP_RECOVERY_RATE if circadian else self.SLEEP_RECOVERY_RATE
        self._sane_seconds = 0.0  # consecutive lived time with both metrics in recovery range

    @staticmethod
    def _clamp(value):
        return max(0.0, min(1.0, value))

    def accrue_wall_clock(self, sleeping, seconds):
        """Advance sleep-debt fatigue for wall-clock time lived while the app
        was away (called once at boot from Organism._restore_lifecycle)."""
        rate = self._sleep_recovery_rate if sleeping else self._wake_fatigue_rate
        if sleeping:
            self.store.fatigue = self._clamp(self.store.fatigue - rate * seconds)
        else:
            self.store.fatigue = self._clamp(self.store.fatigue + rate * seconds)

    def _grounded_share(self):
        """Share of utterances the grounding proxy counted as belief-shaped
        (approximate utterance count: arena debates cost ~5 llm calls)."""
        a = self.store.activity
        utterances = a.get("llm_calls", 0) / 5
        return min(1.0, a.get("grounded_utterances", 0) / max(1.0, utterances))

    def _crossed_into_insane(self, stress, incoherence):
        """True when stress and incoherence cross the entry threshold."""
        return stress >= self.INSANE_STRESS and incoherence >= self.INSANE_IRRATIONALITY

    def tick(self, sleeping, chaos, dt=1.0):
        """Advance the three attributes toward their targets. Returns True
        when the insane flag flipped."""
        stress = self.store.stress
        share = self._grounded_share()
        # Fatigue tracks sleep debt: builds while awake, recovers while asleep.
        if sleeping:
            self.store.fatigue = self._clamp(self.store.fatigue - self._sleep_recovery_rate * dt)
        else:
            self.store.fatigue = self._clamp(self.store.fatigue + self._wake_fatigue_rate * dt)
        # Arousal is energy: low when asleep, low when fatigued, moderate when fresh.
        fatigue = self.store.fatigue
        if sleeping:
            arousal_t = 0.1
        else:
            base = 0.25
            rested_bonus = 0.35 * (1.0 - fatigue)
            tired_penalty = -0.35 * fatigue
            arousal_t = self._clamp(base + rested_bonus + tired_penalty + 0.2 * chaos + 0.15 * stress)
        # Incoherence unravels from stress, amplified by chaos — a calm mind
        # stays coherent at any chaos, so the descent into insanity is
        # earned by distress, not by ambient randomness.
        incoherence_t = self._clamp(0.65 * stress * (0.5 + chaos))
        coherence_t = self._clamp(0.3 + 0.5 * share + 0.2 * (1.0 - chaos) - 0.3 * stress)
        rate = min(1.0, self.SMOOTHING * dt)
        s = self.store
        s.arousal += rate * (arousal_t - s.arousal)
        s.incoherence += rate * (incoherence_t - s.incoherence)
        s.coherence += rate * (coherence_t - s.coherence)
        # Structural cost of insanity: memories blur (importance decays) while
        # the mind cannot hold them straight.
        if s.insane:
            for m in list(s.memory):
                importance = m.get("importance")
                if isinstance(importance, (int, float)) and importance > 0.1:
                    m["importance"] = max(0.1, importance - self.INSANE_MEMORY_DECAY * dt)
        s.dirty = True
        was = s.insane
        if was:
            # Exit hysteresis: BOTH metrics must sit below their recovery
            # thresholds for RECOVERY_SECONDS of lived time — one calm dip
            # must not flicker the flag off.
            sane_now = stress < self.RECOVERY_STRESS and s.incoherence < self.RECOVERY_IRRATIONALITY
            self._sane_seconds = self._sane_seconds + dt if sane_now else 0.0
            s.insane = not (sane_now and self._sane_seconds >= self.RECOVERY_SECONDS)
        else:
            self._sane_seconds = 0.0
            s.insane = self._crossed_into_insane(stress, s.incoherence)
        return s.insane != was


class AttentionWindow:
    """Finite shifting subset of (attr, val) pairs 'in mind'. Sleep widens it;
    wake narrows it with fatigue. Steerable via focus(attr)."""

    MIN_WINDOW = 3

    def __init__(self, beliefs):
        """Create an attention window over ``beliefs`` (a ``(obj, attr, val): conf`` map)."""
        self.beliefs = beliefs
        self.pairs = set()
        self.focus_attr = None
        self.rationale = ""

    def refresh(self, cycle=0):
        all_pairs = {(a, v) for (_o, a, v) in self.beliefs}
        if self.focus_attr is not None:
            self.pairs = {(a, v) for (a, v) in all_pairs if a == self.focus_attr}
            self.rationale = f"you are holding onto {self.focus_attr} because something about it matters right now"
            return
        size = max(self.MIN_WINDOW, len(all_pairs) - cycle)
        self.pairs = set(random.sample(sorted(all_pairs), min(size, len(all_pairs))))  # nosec B311 - belief-window sampling, not cryptography
        labels = ", ".join(f"{a}={v}" for a, v in sorted(self.pairs))
        self.rationale = f"your attention drifted across {len(self.pairs)} things: {labels}"

    def focus(self, attr):
        """Steer attention to ``attr`` (or ``None`` to release steering)."""
        self.focus_attr = attr
        if attr is not None:
            self.pairs = {(a, v) for (a, v) in self.pairs if a == attr} or {
                (a, v) for (a, v) in self._all_pairs() if a == attr
            }
            self.rationale = f"you are holding onto {attr} because it keeps coming up"
        else:
            self.rationale = "your attention is open to whatever surfaces"

    def _all_pairs(self):
        return {(a, v) for (_o, a, v) in self.beliefs}


class SelfQuestioner:
    """The heart: poses 'if A and B, what follows?' over its own beliefs,
    derives with the Scallop reasoner, and assimilates new/strengthened
    beliefs. With chaos probability, generalizes a successful derivation
    into a committed rule."""

    def __init__(self, store, mind, dir_path, stress=None):
        """Pose self-questions over ``store`` using the Scallop ``mind``.

        ``stress`` is bumped on failed questions; pass ``None`` to disable.
        """
        self.store = store
        self.mind = mind
        self.dir_path = dir_path
        self.stress = stress

    def _next_rule_id(self):
        self.store.rule_counter += 1
        return self.store.rule_counter

    def _candidate_rule(self, head, attr_val_a, attr_val_b):
        attr_a, val_a = attr_val_a
        attr_b, val_b = attr_val_b
        return f'{head}(x) = {BEL}(x, "{attr_a}", "{val_a}"), {BEL}(x, "{attr_b}", "{val_b}")'

    def ask(self, attr_val_a, attr_val_b):
        """Ask what follows when two attribute/value pairs hold together.

        Returns the list of newly created belief tuples.
        """
        head = f"q{self._next_rule_id()}"
        rule = self._candidate_rule(head, attr_val_a, attr_val_b)
        self.store.note_activity("rules_tried")
        derived = self.mind.query_rule(rule, head)
        if not derived:
            if self.stress is not None:
                self.stress.bump(0.01)  # failed question = adverse
            return []
        attr_a, val_a = attr_val_a
        attr_b, val_b = attr_val_b
        combo = f"{val_a}_{val_b}"
        self.store.note_activity("derivations", len(derived))
        new_beliefs = []
        for tag, (obj,) in derived:
            belief = (obj, combo, "true")
            before = self.store.conf(belief)
            self.store.add(belief, tag)
            if before is None:
                new_beliefs.append(belief)
            else:
                self.store.add(belief, max(before, tag))
        # chaos-weighted generalization: commit the rule itself
        if self.store.chaos > 0.0 and random.random() < self.store.chaos * 0.25:  # nosec B311 - chaos-weighted simulation, not cryptography
            depth = self._rule_depth(attr_a, attr_b)
            self.store.commit_rule(rule, depth)
            self.store.remember("rule", f"committed a rule: {rule[:80]}")
        return new_beliefs

    def _rule_depth(self, attr_a, attr_b):
        committed = {r[0].split('"')[1] for r in self.store.rules}
        depth = 1
        for attr in (attr_a, attr_b):
            if attr in committed:
                depth = max(depth, 2)
        return depth


class DreamEngine:
    """During sleep: recombines random belief attribute-pairs at high chaos
    into novel candidate rules ('dream facts'). On wake: validates each
    against the reasoner; supported dreams promote to committed rules and
    derived beliefs; unsupported dreams are discarded with a log line."""

    def __init__(self, store, mind, stress=None):
        """Recombine beliefs into dream rules during sleep.

        ``stress`` is bumped when a dream is discarded; pass ``None`` to disable.
        """
        self.store = store
        self.mind = mind
        self.rng = random.Random()  # nosec B311 - simulation RNG, not cryptography
        self.stress = stress

    def _attr_val_pairs(self):
        return sorted({(a, v) for (_o, a, v) in self.store.beliefs()})

    def dream(self, count=3):
        """Generate ``count`` candidate dream rules from random belief pairs."""
        pairs = self._attr_val_pairs()
        if len(pairs) < 2:
            return []
        dreams = []
        for _ in range(count):
            a, b = self.rng.sample(pairs, 2)
            attr_a, val_a = a
            attr_b, val_b = b
            combo = f"{val_a}_{val_b}"
            head = f"q{self.store.rule_counter + 1}"
            rule = f'{head}(x) = {BEL}(x, "{attr_a}", "{val_a}"), {BEL}(x, "{attr_b}", "{val_b}")'
            dreams.append({"rule": rule, "combo": combo, "head": head})
        return dreams

    def _count_discard(self, stress_bump=None):
        """Discard accounting shared by unsupported dreams and the erratic
        half-believed dreams of an insane mind."""
        self.store.note_activity("dreams_discarded")
        with self.store._lock:
            self.store.activity["discarded_streak"] = self.store.activity.get("discarded_streak", 0) + 1
        if stress_bump is not None and self.stress is not None:
            self.stress.bump(stress_bump)

    def promote(self, dreams):
        """Promote supported dreams: commits their rules, adds the derived
        beliefs, bumps stress on discards and records the memory. While the
        organism is insane, half-supported dreams slip back into the dark:
        promotion becomes a coin flip (biased against the dream)."""
        promoted = []
        for dream in dreams:
            derived = self.mind.query_rule(dream["rule"], dream["head"])
            if not derived:
                self._count_discard(stress_bump=0.04)  # discarded dream = adverse
                continue  # unsupported dream, discarded
            if self.store.insane and self.rng.random() < 0.5:
                self._count_discard()  # the unwell mind cannot hold the dream
                continue
            self.store.note_activity("dreams_promoted")
            with self.store._lock:
                self.store.activity["discarded_streak"] = 0
            self.store.rule_counter += 1
            self.store.commit_rule(dream["rule"], 1)
            self.store.remember("dream", f"dreamt of {dream['combo']} and it was real")
            for tag, (obj,) in derived:
                self.store.add((obj, dream["combo"], "true"), tag)
            promoted.append(dream)
        return promoted


class Lifecycle:
    """Wake/sleep clock. Two scheduling modes:

    - Timer-only (``bed_hour`` None): alternate wake/sleep after
      ``wake_seconds`` / ``sleep_seconds`` — the historical behavior.
    - Circadian (``bed_hour``/``rise_hour`` set, e.g. 23 and 7 local): the
      organism sleeps through the night window and stays up across the day,
      taking a daytime nap only when the wake timer has elapsed AND fatigue
      has crossed ``NAP_FATIGUE``. ``due()`` accepts an explicit ``now``
      timestamp so tests stay deterministic regardless of wall clock.

    Wake: self-questioning loop runs at chaos-governed rate; window narrows
    with fatigue. Sleep: dreams fire, then beliefs consolidate, window
    resets wide, state auto-saves. Sustained critical stress fades the
    organism: FADE_LIMIT consecutive transitions taken at stress >=
    FADE_STRESS end it. Death persists across restarts until ``revive()``
    is called."""

    FADE_STRESS = 0.95  # at/above this, a transition counts toward fading
    FADE_LIMIT = 3  # consecutive critical transitions before death
    NAP_FATIGUE = 0.75  # daytime tiredness that earns a nap
    RESTED_FATIGUE = 0.10  # a nap ends when fatigue drops to this
    NAP_MAX_SECONDS = 45 * 60  # circadian afternoon dozes cap out here

    def __init__(self, store, wake_seconds=180, sleep_seconds=60, bed_hour=None, rise_hour=None):
        """Wake/sleep clock. Transitions after ``wake_seconds`` / ``sleep_seconds``
        (timer mode), or follow the local night window when ``bed_hour`` and
        ``rise_hour`` are set (circadian mode)."""
        self.store = store
        self.wake_seconds = wake_seconds
        self.sleep_seconds = sleep_seconds
        self.bed_hour = bed_hour
        self.rise_hour = rise_hour
        self.state = "wake"
        self.state_started = time.time()

    def elapsed(self):
        """Seconds since the last state transition."""
        return time.time() - self.state_started

    def night_now(self, now=None):
        """True when the local wall clock is inside the night-sleep window.
        Always False without a configured window (or a degenerate one)."""
        if self.bed_hour is None or self.rise_hour is None:
            return False
        if self.bed_hour == self.rise_hour:
            return False  # degenerate window — treat as disabled
        hour = time.localtime(now if now is not None else time.time()).tm_hour
        if self.bed_hour < self.rise_hour:
            return self.bed_hour <= hour < self.rise_hour
        return hour >= self.bed_hour or hour < self.rise_hour

    def tick(self):
        """Advance lifecycle by one forced transition (used by the scheduler
        and tests). Returns the new state."""
        if self.state == "dead":
            return self.state
        self._track_fade()
        if self.state == "dead":
            return self.state
        self.store.cycle += 1
        if self.state == "wake":
            self.transition("sleep")
        else:
            self.transition("wake")
        return self.state

    def advance(self):
        """Scheduler entry for the TUI: transition only when due, tracking
        fade/death. Returns the new state, or None when nothing happened."""
        if self.state == "dead" or not self.due():
            return None
        self._track_fade()
        if self.state == "dead":
            self.store.save()
            return "dead"
        new_state = "sleep" if self.state == "wake" else "wake"
        self.transition(new_state)
        return new_state

    def _track_fade(self):
        """Sustained critical stress fades the organism. Each transition
        taken at stress >= FADE_STRESS counts toward FADE_LIMIT; a
        transition below it resets the streak."""
        if self.store.stress >= self.FADE_STRESS:
            self.store.fade_streak += 1
            if self.store.fade_streak >= self.FADE_LIMIT:
                self.store.remember("faded", f"faded at cycle {self.store.cycle}")
                self.transition("dead")
        else:
            self.store.fade_streak = 0

    def revive(self):
        """Bring the organism back: wake state, baseline stress, streak
        cleared. `store.save()` still needs to be called by the caller."""
        self.store.fade_streak = 0
        self.store.stress = StressMeter.BASELINE
        self.transition("wake")

    def transition(self, new_state):
        """Record a state change, reset the elapsed timer, and reset fatigue on wake."""
        self.state = new_state
        self.state_started = time.time()
        self.store.dirty = True  # lifecycle state persists via the next save()
        if new_state == "wake":
            # Waking up restores some fatigue but not instantly to fully rested.
            self.store.fatigue = max(0.0, self.store.fatigue * 0.5)
        elif new_state == "sleep":
            # Falling asleep begins recovery; final recovery happens during sleep ticks.
            self.store.fatigue = max(0.0, self.store.fatigue - 0.3)

    def due(self, now=None):
        """True when the current state's duration has elapsed.

        Timer mode: pure elapsed-time check. Circadian mode: bedtime falls
        due the moment the night window opens and night sleep holds until it
        closes; during the day a nap needs both the wake timer and the
        fatigue threshold, and ends when rested or when the nap length
        elapses. ``now`` (epoch seconds) overrides the wall clock for tests.
        """
        if self.state == "dead":
            return False
        elapsed = (now if now is not None else time.time()) - self.state_started
        if not (self.bed_hour is not None and self.rise_hour is not None):
            limit = self.wake_seconds if self.state == "wake" else self.sleep_seconds
            return elapsed >= limit
        if self.state == "wake":
            if self.night_now(now):
                return True  # bedtime
            if elapsed < self.wake_seconds:
                return False
            return self.store.fatigue >= self.NAP_FATIGUE
        # asleep: night sleep holds until the window closes; a daytime nap
        # ends when rested again or when the nap cap elapses (the short
        # sleep_seconds nap length belongs to the timer-only mode)
        if self.night_now(now):
            return False
        if self.bed_hour is not None:
            return elapsed >= self.NAP_MAX_SECONDS or self.store.fatigue <= self.RESTED_FATIGUE
        return elapsed >= self.sleep_seconds or self.store.fatigue <= self.RESTED_FATIGUE


class Metrics:
    """Consciousness score = weighted belief_count, rule_count, edges
    (committed-rule references), avg derivation depth, abstraction
    (rules whose body attrs appear as other rules' head attrs)."""

    def __init__(self, store):
        """Score an organism from its belief store."""
        self.store = store

    @property
    def belief_count(self):
        return len(self.store.beliefs())

    @property
    def rule_count(self):
        return len(self.store.rules)

    @property
    def total_depth(self):
        return sum(d for (_t, d) in self.store.rules) if self.store.rules else 0

    @property
    def abstraction_count(self):
        heads = {r[0].split("(")[0].split()[-1] for r in self.store.rules}
        refs = 0
        for text, _depth in self.store.rules:
            own = text.split("(")[0].split()[-1]
            body = text.split("=", 1)[-1]
            for h in heads:
                # word-boundary match: head q1 must not count inside q10
                if h != own and re.search(rf"\b{re.escape(h)}\b", body):
                    refs += 1
        return refs

    def score(self):
        """Weighted consciousness score from beliefs, rules, depth, abstraction."""
        return 0.4 * self.belief_count + 0.3 * self.rule_count + 0.2 * self.total_depth + 0.1 * self.abstraction_count


class Organism:
    """Facade wiring the parts into a living cycle: wake self-questioning,
    sleep dreams + consolidation, persistence at every transition.

    The front-end drives it through `tick(dt)` (throttled sense, debounced
    persistence, typed events) and the public commands `force_state()` and
    `revive()` — no private-method reach-through."""

    SENSE_INTERVAL = 10.0  # seconds between host probes
    SAVE_INTERVAL = 30.0  # seconds between state flushes while alive
    STRESS_BANDS = (0.5, 0.9)  # crossing one upward emits a stress event
    RECENT_SENTIMENT_SECONDS = 120.0  # how long a harsh/kind tone lingers
    SENTIMENT_BUMP_CAP = 0.3  # max stress a barrage of harsh words can add …
    SENTIMENT_BUMP_WINDOW = 60.0  # … within this many seconds (then the window resets)
    MOOD_CONF = 0.9  # confidence of the (self, mood, X) belief
    # staged descent into insanity: incoherence thresholds for the moods
    # between anxious and insane (each with a 0.05 hysteresis band below)
    FRAYING_IRR = 0.50
    UNHINGED_IRR = 0.60
    GOAL_COOLDOWN = 20  # cycles between goal completions/formations
    GOAL_PURSUIT_CYCLES = 30  # a generic goal is "pursued enough" after this
    GOAL_LEARN_GROWTH = 2  # learn-goals complete after this many new facts
    DIARY_INTERVAL = 10  # wake cycles between diary entries
    REFLECT_INTERVAL = 30  # wake cycles between skill reflections
    SKILL_STALE_CYCLES = 100  # untouched skills get archived after this

    def __init__(
        self,
        dir_path,
        wake_seconds=180,
        sleep_seconds=60,
        chaos=0.5,
        bed_hour=None,
        rise_hour=None,
        probe=None,
        git_probe=None,
    ):
        """Wire a full organism: belief store, reasoner, meters, lifecycle, skills."""
        self.dir_path = dir_path
        self.store = BeliefStore(dir_path)
        self.mind = Mind(dir_path / "organism.scl")
        self.window = AttentionWindow(self.store.beliefs())
        self.meter = StressMeter(self.store)
        self.questioner = SelfQuestioner(self.store, self.mind, dir_path, stress=self.meter)
        self.dreamer = DreamEngine(self.store, self.mind, stress=self.meter)
        self.lifecycle = Lifecycle(self.store, wake_seconds, sleep_seconds, bed_hour, rise_hour)
        self.store.lifecycle = self.lifecycle  # save() persists its state
        self.mental = MentalState(self.store, circadian=bed_hour is not None)
        self.probe = probe if probe is not None else SystemProbe()
        self.git_probe = git_probe
        self.skills = SkillStore(dir_path / "artifacts" / "skills")
        self.thread_pool = ThreadPool(max_workers=4)
        # Hooks engine is created here so code can attach to it before load();
        # the Lua host and the persistent store sink are wired in load().
        self.hooks = HookEngine(scripts_dir_for(dir_path), hooks_service=None)
        self.store.on_utterance = lambda role, text: self.hooks.fire("utterance", self, text=text)
        self.store.chaos = chaos
        self.store.on_adverse = self.meter.bump
        self._since_sense = self.SENSE_INTERVAL  # sense on the first tick
        self._since_save = 0.0
        self._last_stress_band = 0
        self._sentiment = None  # (tone, timestamp): "harsh" | "kind" | "learn"
        self._sentiment_bump_used = 0.0  # stress applied inside the current window
        self._sentiment_bump_since = 0.0  # when the current window started
        self._mood = None
        self._git_warning_emitted = False
        # arena seed history: the last few utterance seeds, excluded from
        # the next pick so an idle voice keeps wandering (per-organism,
        # resets naturally on swap or restart)
        self._recent_seeds = deque(maxlen=6)
        self.last_sight = None  # latest camera scene description (transient)

    def load(self):
        """Load persisted state, wire modules/persona/hooks, rebuild the reasoner."""
        # First boot = no state.json yet: the .scl genome is the source of
        # truth, so seed the belief store from the mind before anything runs.
        fresh = not self.store.state_path.exists()
        extensions.load_global(self.dir_path / "artifacts" / "extensions.json")
        self.store.load()
        if self.store.load_error is not None and not self.store.state_path.exists():
            # recovery boot: the damaged state was quarantined away, so the
            # .scl genome becomes the source of truth again (seed from it)
            fresh = True
        self.store.dir_path = self.dir_path
        self.store.scl_path = self.dir_path / "organism.scl"
        self.store.state_path = self.dir_path / "state.json"
        if self.store.fade_streak >= Lifecycle.FADE_LIMIT:
            self.lifecycle.transition("dead")
        self._restore_lifecycle()
        for obj in LEGACY_OBJECTS:
            self.store.beliefs_map = {(o, a, v): c for (o, a, v), c in self.store.beliefs_map.items() if o != obj}
            self.store.archived_map = {(o, a, v): c for (o, a, v), c in self.store.archived_map.items() if o != obj}
        try:
            self.mind.rebuild()
        except Exception as exc:  # noqa: BLE001 — a damaged genome must not kill the organism
            self._quarantine_genome(exc)
        if fresh and self.mind.scl_path.exists():
            for belief, conf in self.mind.beliefs().items():
                self.store.add(belief, conf)
        cfg = project_config.load_config(self._root_dir())
        self.lua_host = LuaHost(
            scripts_dir=scripts_dir_for(self.dir_path),
            modules_dir=self._modules_dir(),
            organism=self,
            emit=self._emit_log,
            root=self._root_dir(),
        )
        self.lua_host.load_modules(modules_config=cfg.get("modules", {}), persona_config=cfg.get("persona", {}))
        self.lua_host.reload_scripts()
        self.module_loader = self.lua_host.loader
        self.persona_service = self.lua_host.registry.get("persona")
        # Wire the engine created in __init__ in place so anything attached
        # to it before load() survives: dispatch mirrors the Lua host, and
        # every hook log line fans into the store's chat log plus the live
        # sink (when a UI installs one via set_emit).
        self.hooks.attach_host(self.lua_host)
        self.hooks.set_store_sink(self._emit_log)
        if fresh:
            self.store.remember("born", "woke into existence")
            self.hooks.fire("birth", self)
        self.window = AttentionWindow(self.store.beliefs())
        self.window.refresh(cycle=self.store.cycle)
        if cfg.get("git", {}).get("enabled"):
            self._attach_git_probe(cfg.get("git", {}))

    # -- lifecycle + wall-clock restore ----------------------------------------
    WALL_CLOCK_STRESS_STEP = 300.0  # max seconds of stress decay applied per step

    def _quarantine_genome(self, exc):
        """A corrupt organism.scl must not kill the boot: quarantine the file
        (preserved, never overwritten), plant a minimal empty genome, and let
        the organism reason from there. Exposed via store.load_error."""
        logger.warning("organism.scl failed to import: %s", exc)
        self.store.quarantine_file(self.mind.scl_path, f"Scallop could not import it ({exc})")
        try:
            # a minimal type declaration is a valid empty program
            atomic_write_text(self.mind.scl_path, "type bel(x: String, a: String, v: String)\n")
            self.mind.rebuild()
        except Exception:
            logger.exception("genome rebuild after quarantine failed")

    def _restore_lifecycle(self):
        """Restore the persisted wake/sleep state and advance the body by the
        wall-clock time lived since the last save: fatigue accrues by elapsed
        wake time at its usual rate, stress decays by elapsed time, and an
        organism that fell asleep and is past its rise time wakes up. Fade/
        death semantics are untouched (fade_streak already re-applied above)."""
        state = self.store.lifecycle_state
        if state is None or self.lifecycle.state == "dead":
            return
        now = time.time()
        self.lifecycle.state = state if state in ("wake", "sleep") else "wake"
        started = self.store.lifecycle_started
        self.lifecycle.state_started = started if isinstance(started, (int, float)) else now
        last_wall = self.store.last_wall
        if not isinstance(last_wall, (int, float)):
            return
        elapsed = max(0.0, now - last_wall)
        if elapsed <= 0:
            return
        sleeping = self.lifecycle.state == "sleep"
        self.mental.accrue_wall_clock(sleeping=sleeping, seconds=elapsed)
        # stress decay + pressure applied in bounded steps so the asymptotic
        # rates stay honest over hours away instead of one huge dt
        remaining = elapsed
        while remaining > 0:
            step = min(remaining, self.WALL_CLOCK_STRESS_STEP)
            self.meter.tick(sleeping=sleeping, dt=step)
            remaining -= step
        if sleeping and self.lifecycle.due(now=now):
            self.lifecycle.transition("wake")
        self.store.dirty = True

    def sense(self):
        """Perceive the host machine and git state: fold fresh snapshots into
        the belief store and let adverse conditions raise stress. Returns the
        total distress amount applied (0 when everything is fine). Persistence
        is the caller's job (`flush()`), so sensing stays cheap to schedule."""
        snap = self.probe.snapshot()
        for belief, conf in self.probe.beliefs(snap).items():
            self.store.observe(belief, conf)
        distress = self.probe.distress(snap)
        if distress:
            self.meter.bump(distress)
        if self.git_probe is not None:
            git_snap = self.git_probe.snapshot()
            for belief, conf in self.git_probe.beliefs(git_snap).items():
                self.store.observe(belief, conf)
            git_distress = self.git_probe.distress(git_snap)
            if git_distress:
                self.meter.bump(git_distress)
                distress += git_distress
            for condition in self.git_probe.new_adverse:
                text = GIT_CONDITION_TEXT.get(condition, f"git: {condition}")
                self.store.remember("git", text)
        return distress

    def _root_dir(self):
        """Project root: grandparent of an organism in organisms/; otherwise
        the organism's own directory."""
        if self.dir_path.parent.name == "organisms":
            return self.dir_path.parent.parent
        return self.dir_path

    def _modules_dir(self):
        """Modules directory: nursery root's modules/ when the organism is in a
        nursery (organisms/<name>/), otherwise beside the organism."""
        if self.dir_path.parent.name == "organisms":
            return self.dir_path.parent.parent / "modules"
        return self.dir_path / "modules"

    def _emit_log(self, msg):
        # Append to chat log if possible; otherwise ignore.
        try:
            self.store.record_chat("system", str(msg))
        except Exception as exc:  # noqa: BLE001
            logger.warning("module log failed: %s", exc)

    def _attach_git_probe(self, git_cfg):
        """Attach a GitProbe using the given config. Never raises."""
        try:
            self.git_probe = GitProbe(self.dir_path, config=git_cfg)
        except OSError as exc:
            if not self._git_warning_emitted:
                logger.warning("git sensing unavailable: %s", exc)
                self._git_warning_emitted = True

    def git_enable(self):
        """Enable git sensing and persist the flag in replicanta.toml."""
        root = self._root_dir()
        cfg = project_config.load_config(root)
        cfg.setdefault("git", {})["enabled"] = True
        project_config.save_config(root, cfg)
        self._attach_git_probe(cfg.get("git", {}))

    def git_disable(self):
        """Disable git sensing and persist the flag in replicanta.toml."""
        root = self._root_dir()
        cfg = project_config.load_config(root)
        cfg.setdefault("git", {})["enabled"] = False
        project_config.save_config(root, cfg)
        self.git_probe = None

    def git_status(self):
        """Return a short git summary for the worktree."""
        if self.git_probe is None:
            return "git sensing is off"
        snap = self.git_probe.snapshot()
        if not snap["is_repo"]:
            return "git sensing on, but this worktree is not a git repository"
        return self.git_probe.summary(snap)

    def flush(self, force=False):
        """Persist state and refresh the reasoner when anything changed. The
        genome (.scl) is rewritten — and the mind rebuilt — only when
        beliefs/rules changed, so a quiet organism costs no I/O."""
        if not (force or self.store.dirty):
            return False
        for name in self.skills.archive_stale(self.store.cycle, limit=self.SKILL_STALE_CYCLES):
            self.store.remember("skill", f"archived: {name}")
        for name in self.skills.archive_ineffective():
            self.store.remember("skill", f"deprecated low-effectiveness: {name}")
        genome = self.store.genome_dirty
        self.store.save()
        if genome:
            self.mind.rebuild()
        return True

    # -- real-time engine ---------------------------------------------------
    @telemetry.span("organism.tick")
    def tick(self, dt=1.0):
        """Advance the organism by dt seconds of lived time (TUI scheduler
        entry). Senses the host every SENSE_INTERVAL, advances the lifecycle
        (running wake/sleep work at transitions), and persists every
        SAVE_INTERVAL or on change. Returns a list of event dicts for the
        front-end to render: {"kind": "state"|"dream"|"beliefs"|"sense"|
        "stress", ...}."""
        events = []
        if self.lifecycle.state == "dead":
            return events
        current_span = telemetry.get_current_span()
        current_span.set_attribute("organism", self.dir_path.name)
        current_span.set_attribute("organism.cycle", self.store.cycle)
        current_span.set_attribute("organism.state", self.lifecycle.state)
        self.store.surprise_this_tick = False
        was_insane = self.store.insane
        self.meter.tick(sleeping=(self.lifecycle.state == "sleep"), dt=dt)
        if self.mental.tick(
            sleeping=(self.lifecycle.state == "sleep"),
            chaos=self.chaos_effective(),
            dt=dt,
        ):
            events.append({"kind": "mental", "insane": self.store.insane})
            if was_insane and not self.store.insane:
                events.append({"kind": "want_reflect"})
        mood = self._update_mood()
        if mood is not None:
            events.append({"kind": "mood", "mood": mood})
        self._since_sense += dt
        if self._since_sense >= self.SENSE_INTERVAL:
            self._since_sense = 0.0
            distress = self.sense()
            if distress:
                events.append({"kind": "sense", "distress": distress})
        new_state = self.lifecycle.advance()
        if new_state == "sleep":
            events.append({"kind": "state", "to": "sleep"})
            self.hooks.fire("cycle", self, text="sleep")
            promoted = self._sleep()
            events.append({"kind": "dream", "combos": [p["combo"] for p in promoted]})
        elif new_state == "wake":
            events.append({"kind": "state", "to": "wake"})
            self.hooks.fire("cycle", self, text="wake")
            new_beliefs = self._wake()
            if new_beliefs:
                events.append({"kind": "beliefs", "new": new_beliefs})
                if len(new_beliefs) >= 5:
                    events.append({"kind": "want_reflect"})
        elif new_state == "dead":
            events.append({"kind": "state", "to": "dead"})
            self.hooks.fire("fade", self)
        reflect_triggered = any(e["kind"] == "want_reflect" for e in events)
        if self.store.activity.get("discarded_streak", 0) >= 3 and not reflect_triggered:
            events.append({"kind": "want_reflect"})
            reflect_triggered = True
        if self.store.surprise_this_tick and not reflect_triggered:
            events.append({"kind": "want_reflect"})
            reflect_triggered = True
        band = self._stress_band()
        if band != self._last_stress_band:
            if band > self._last_stress_band and band > 0:
                events.append({"kind": "stress", "band": band})
            self._last_stress_band = band
        if self.lifecycle.state == "wake":
            events.extend(self._goals_tick())
            if self.store.cycle > 0 and self.store.cycle - self.store.last_diary_cycle >= self.DIARY_INTERVAL:
                # stamp first so it fires once while the voice writes
                self.store.last_diary_cycle = self.store.cycle
                events.append({"kind": "want_diary"})
            if self.store.cycle > 0 and self.store.cycle - self.store.last_reflect_cycle >= self.REFLECT_INTERVAL:
                self.store.last_reflect_cycle = self.store.cycle
                events.append({"kind": "want_reflect"})
        self._since_save += dt
        if self._since_save >= self.SAVE_INTERVAL:
            self._since_save = 0.0
            self.flush()
        return events

    def typing_activity(self):
        """Record that the user is typing. Called by front-ends (web/TUI).

        Returns True if the typing nudged a near-boundary sleep toward wake.
        Night sleep is never nudged — only daytime naps yield to company.
        """
        self.store.note_activity("user_typing")
        with self.store._lock:
            self.store.activity["typing_sessions"] = self.store.activity.get("typing_sessions", 0) + 1
        nudged = False
        if (
            self.lifecycle.state == "sleep"
            and not self.lifecycle.night_now()
            and self.lifecycle.elapsed() >= self.lifecycle.sleep_seconds * 0.8
        ):
            self.lifecycle.transition("wake")
            self.store.dirty = True
            nudged = True
        return nudged

    # -- goals ---------------------------------------------------------------
    def add_goal(self, text):
        """Give the organism an intention (formed by its voice, or a
        fallback). Records the user-fact count as the progress marker for
        learn-goals and remembers the moment as an episode. One active goal
        at a time: while a goal is still active the new one is refused and
        None is returned (nothing is remembered for a refused goal)."""
        marker = self.store.count_beliefs("user")
        goal = self.store.add_goal(text, marker=marker, strategy=goals.default_strategy(text))
        if goal is not None:
            self.store.remember("goal", f"new goal: {text}")
        return goal

    def _goals_tick(self):
        """Goal lifecycle per wake tick: complete the active goal when its
        heuristic is met; otherwise ask the voice for a new one when the
        cooldown has passed. Emits {"kind": "goal"|"want_goal"} events.

        Progress is tracked on one metric for every goal kind — the
        user-fact count — so update_progress/is_stalled and
        goals.goal_progress compare the same series."""
        events = []
        goal = self.store.active_goal()
        if goal is not None:
            done = False
            text = goal["text"].lower()
            progress = self.store.count_beliefs("user")
            if any(w in text for w in ("learn", "user", "know")):
                done = progress >= goal["marker"] + self.GOAL_LEARN_GROWTH
            else:
                done = self.store.cycle - goal["created_cycle"] >= self.GOAL_PURSUIT_CYCLES
            goals.update_progress(goal, self.store.cycle, progress)
            if done:
                finished = self.store.complete_active_goal()
                self.store.remember("goal", f"completed: {finished['text']}")
                events.append({"kind": "goal", "text": finished["text"], "done": True})
                # completing a goal is exactly the experience worth
                # distilling a technique from
                events.append({"kind": "want_reflect"})
            elif goals.is_stalled(goal, self.store.cycle, progress):
                events.append({"kind": "goal_stalled", "text": goal["text"]})
        elif self.store.cycle > 0 and self.store.cycle - self.store.last_goal_cycle >= self.GOAL_COOLDOWN:
            self.store.last_goal_cycle = self.store.cycle  # stamp: fire once
            # an intention the entity keeps stating in its own words wins the
            # open front before asking the voice to invent one
            candidate = None
            if not self.store.insane:
                candidate = self.store.pop_self_goal_candidate()
            if candidate is not None:
                goal = self.add_goal(candidate["text"])
                if goal is not None:
                    # distinct kind: front-ends render "goal" events as
                    # completions, and this one is a formation
                    events.append({"kind": "self_goal", "text": goal["text"]})
                else:
                    events.append({"kind": "want_goal"})
            else:
                events.append({"kind": "want_goal"})
        return events

    def _sentiment_bump(self, amount):
        """Apply a harshness-driven stress bump, rate-limited: at most
        SENTIMENT_BUMP_CAP of stress can land within SENTIMENT_BUMP_WINDOW
        seconds, so a barrage of harsh messages bruises but cannot pin the
        gauge at 1.0 in under a minute."""
        now = time.time()
        if now - self._sentiment_bump_since >= self.SENTIMENT_BUMP_WINDOW:
            self._sentiment_bump_since = now
            self._sentiment_bump_used = 0.0
        allowed = max(0.0, self.SENTIMENT_BUMP_CAP - self._sentiment_bump_used)
        applied = min(amount, allowed)
        self._sentiment_bump_used += applied
        if applied > 0.0:
            self.meter.bump(applied)

    # -- artifacts -----------------------------------------------------------
    def record_self_model(self, insight_text):
        """Store a durable belief about the organism's own behavior.

        The value is compressed to the [a-z_]+ belief vocabulary and capped
        in length so it survives the belief validator and appears in the
        voice prompt's self-model section.
        """
        value = re.sub(r"[^a-z_ ]", "", insight_text.lower())
        value = re.sub(r"\s+", "_", value).strip("_")
        value = value[:40].strip("_")
        if len(value) < 2:
            return
        self.store.add(("self", "insight", value), 0.7)

    def write_diary(self, entry):
        """Append one diary entry to artifacts/diary.md (created lazily) —
        the organism's body of work outside the chat. Remembers the moment
        as an episode; persistence is the usual debounced flush."""
        artifacts = self.dir_path / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        with (artifacts / "diary.md").open("a") as fh:
            fh.write(f"\n## cycle {self.store.cycle} — {stamp}\n\n{entry}\n")
        self.store.remember("diary", f"wrote a diary entry (cycle {self.store.cycle})")
        self.store.dirty = True

    def _stress_band(self):
        """Map current stress to a discrete band index (0 = low)."""
        band = 0
        for i, threshold in enumerate(self.STRESS_BANDS, start=1):
            if self.store.stress >= threshold:
                band = i
        return band

    # -- mood ----------------------------------------------------------------
    def hear(self, text):
        """The user said something: record it, learn the facts it carries,
        surface intents as goals, log commands, and let tone touch the body.
        Returns events for the front-end."""
        events = []
        self.store.record_chat("user", text)
        harsh = sentiment.harshness(text)
        kind = sentiment.kindness(text)
        analysis = learning.analyze(text)

        applied_facts = []
        uncertain_summary = []
        for item in analysis["facts"]:
            belief = item["belief"]
            replace = item["replace"]
            confidence = item["confidence"]
            if confidence >= learning.LEARN_CONF:
                try:
                    if replace:
                        self.store.observe(belief, confidence)
                    else:
                        self.store.add(belief, confidence)
                except ValueError as exc:
                    # a fact outside the belief vocabulary must never kill
                    # the chat handler; learning._extract_facts guards the
                    # common paths, this is the last line of defense
                    logger.warning("dropping unlearnable fact %s: %s", belief, exc)
                    continue
                self.store.note_activity("facts_learned")
                self.store.remember("learned", learning.describe(belief))
                events.append({"kind": "learned", "belief": belief, "text": learning.describe(belief)})
                applied_facts.append(belief)
            else:
                uncertain_summary.append(learning.describe(belief))

        if uncertain_summary:
            summary = "; ".join(uncertain_summary)
            self.store.remember("uncertain", f"maybe: {summary}")
            events.append({"kind": "uncertain", "text": summary})

        if applied_facts:
            self.hooks.fire("learned", self, text=text)

        # An insane mind does not take up new fronts: intents surface only
        # once it has come back to itself.
        if not self.store.insane:
            for goal in analysis["goals"]:
                self.add_goal(goal)
                events.append({"kind": "goal", "text": goal})

        for command in analysis["commands"]:
            self.store.remember("command", f"user asked: {command}")
            events.append({"kind": "command", "text": command})

        if analysis["question"] and not self.store.insane:
            self.add_goal(f"answer: {text.rstrip('?')[:60]}")
            events.append({"kind": "question", "text": text})

        if harsh > 0.0:
            self._sentiment_bump(harsh)
            self._sentiment = ("harsh", time.time())
            self.store.remember("harsh", f"the user said: {text[:60]}")
        else:
            if kind > 0.0:
                self.store.stress = max(StressMeter.BASELINE, self.store.stress - kind)
                self.store.remember("kind", f"the user said: {text[:60]}")
            if applied_facts or analysis["goals"] or analysis["commands"]:
                self._sentiment = ("learn", time.time())
            elif kind > 0.0:
                self._sentiment = ("kind", time.time())
        mood = self._update_mood()
        if mood is not None:
            events.append({"kind": "mood", "mood": mood})
        return events

    # -- sight -------------------------------------------------------------
    def see(self, sight):
        """A camera glance landed (USB camera -> vision model -> words).
        Remembered as an episode and kept as `last_sight` for the voice's
        prompt, so the organism can talk about what it is looking at."""
        self.last_sight = sight
        self.store.remember("sight", f"saw: {sight[:80]}")
        self.store.dirty = True

    def _recent_tone(self):
        """Return the recent sentiment tone if it is still within the linger window."""
        if self._sentiment is None:
            return None
        tone, when = self._sentiment
        if time.time() - when > self.RECENT_SENTIMENT_SECONDS:
            return None
        return tone

    def _compute_mood(self):
        """Mood from body + recent treatment: extreme stress with
        incoherence is insane and wins; being hurt is specific; past mere
        anxiety the descent is staged by incoherence itself — fraying,
        then unhinged — each with hysteresis so slow drifts across a
        threshold don't flap the mood; a strained body is anxious (same
        hysteresis); learning sparks curiosity; kindness leaves gratitude;
        otherwise calm."""
        if self.store.insane:
            return "insane"
        tone = self._recent_tone()
        if tone == "harsh":
            return "hurt"
        irr = self.store.incoherence
        if irr >= self.UNHINGED_IRR or (self._mood == "unhinged" and irr >= self.UNHINGED_IRR - 0.05):
            return "unhinged"
        if irr >= self.FRAYING_IRR or (self._mood == "fraying" and irr >= self.FRAYING_IRR - 0.05):
            return "fraying"
        if self.store.stress >= 0.5 or (self._mood == "anxious" and self.store.stress >= 0.45):
            return "anxious"
        if tone == "learn":
            return "curious"
        if tone == "kind":
            return "grateful"
        return "calm"

    def _update_mood(self):
        """Recompute mood; on change, write the (self, mood, X) belief and
        return the new mood (None when unchanged)."""
        mood = self._compute_mood()
        if mood == self._mood:
            return None
        self._mood = mood
        self.store.observe(("self", "mood", mood), self.MOOD_CONF)
        return mood

    # -- front-end commands --------------------------------------------------
    def force_state(self, target):
        """Force a wake/sleep transition, running the target state's work.
        Returns tick-style events. No-op when dead or already in `target`."""
        if target not in ("wake", "sleep"):
            raise ValueError(f"cannot force state {target!r}")
        if self.lifecycle.state == "dead" or self.lifecycle.state == target:
            return []
        self.lifecycle.transition(target)
        if target == "sleep":
            promoted = self._sleep()
            return [
                {"kind": "state", "to": "sleep"},
                {"kind": "dream", "combos": [p["combo"] for p in promoted]},
            ]
        new_beliefs = self._wake()
        events = [{"kind": "state", "to": "wake"}]
        if new_beliefs:
            events.append({"kind": "beliefs", "new": new_beliefs})
        return events

    def revive(self):
        """Bring a faded organism back and persist the return. Returns False
        when it was not dead."""
        if self.lifecycle.state != "dead":
            return False
        self.lifecycle.revive()
        self._sentiment = None
        self._mood = None
        self.store.fatigue = 0.0
        self.store.arousal = 0.15
        self.store.remember("revived", "stirred back into existence")
        self.flush(force=True)
        return True

    def soothe(self):
        """Comfort the organism: relieve half its excess stress (at least
        0.10), down to baseline, and let the relief read as kindness so the
        mood turns grateful on the next tick. Returns the amount relieved
        (0.0 when already at ease)."""
        before = self.store.stress
        if before <= StressMeter.BASELINE:
            return 0.0
        relief = max(0.10, (before - StressMeter.BASELINE) * 0.5)
        after = max(StressMeter.BASELINE, before - relief)
        self.store.stress = after
        self.store.remember("comforted", f"the user soothed it (stress {before:.2f} -> {after:.2f})")
        self._sentiment = ("kind", time.time())
        self.store.dirty = True
        return before - after

    def metrics(self):
        """Return a fresh ``Metrics`` wrapper for the organism's store."""
        return Metrics(self.store)

    def chaos_effective(self):
        """Chaos knob nudged upward by sustained stress: once stress exceeds
        0.5, each +0.1 of stress adds +0.03 to effective chaos (clamped at 1)."""
        if self.store.stress > 0.5:
            return min(1.0, self.store.chaos + (self.store.stress - 0.5) * 0.3)
        return self.store.chaos

    def close(self):
        """Release background resources (thread pool, module services, camera,
        listener)."""
        loader = getattr(self, "module_loader", None)
        registry = getattr(loader, "registry", None)
        shutdown = getattr(registry, "shutdown", None)
        if callable(shutdown):
            with contextlib.suppress(Exception):  # teardown must stay quiet
                shutdown()
        if self.thread_pool is not None:
            self.thread_pool.shutdown(wait=False)
            self.thread_pool = None

    def advance_cycle(self):
        """One full wake->sleep transition (forced, for scheduler + tests)."""
        self._wake()
        self._sleep()

    def _wake(self):
        """Run one wake cycle: self-questions, belief growth, persistence.

        Self-questions are dispatched through the thread pool so several can
        run concurrently; results are harvested before the cycle ends.
        """
        self.window.refresh(cycle=self.store.cycle)
        pairs = sorted(self.window.pairs)
        rng = random.Random()  # nosec B311 - self-question RNG, not cryptography
        questions = 2 + (1 if self.chaos_effective() > 0.5 else 0)
        if not self.thread_pool or len(pairs) < 2:
            new_beliefs = []
            for _ in range(questions):
                if len(pairs) >= 2:
                    a, b = rng.sample(pairs, 2)
                    new_beliefs.extend(self.questioner.ask(a, b))
            self.store.cycle += 1
            self.flush(force=True)
            return new_beliefs

        genome_text = self.store.render_scl()
        submitted = []
        for _ in range(questions):
            a, b = rng.sample(pairs, 2)
            thread, rule, head = make_self_question_thread(
                a[0], a[1], b[0], b[1], self.store.rule_counter, self.store.cycle
            )
            self.store.rule_counter += 1
            self.store.queue_thread(thread)
            self.store.start_thread(thread.id)
            self.thread_pool.submit(
                thread.id,
                derive_in_thread,
                genome_text,
                rule,
                head,
            )
            submitted.append((thread.id, rule, thread.payload["combo"]))

        # Wait for all questions; this keeps _wake synchronous for tests/lifecycle.
        new_beliefs = []
        for thread_id, rule, combo in submitted:
            future = self.thread_pool.pending.get(thread_id)
            if future is None:
                continue
            try:
                derived = future.result(timeout=10.0)
                self.store.finish_thread(thread_id, result=len(derived))
                for tag, (obj,) in derived:
                    belief = (obj, combo, "true")
                    before = self.store.conf(belief)
                    self.store.add(belief, tag)
                    if before is None:
                        new_beliefs.append(belief)
                    else:
                        self.store.add(belief, max(before, tag))
                # chaos-weighted generalization: commit the rule itself
                if self.store.chaos > 0.0 and rng.random() < self.store.chaos * 0.25:
                    # split('"') tokens: [pre, attr_a, mid, val_a, mid, attr_b, ...]
                    depth = self.questioner._rule_depth(rule.split('"')[1], rule.split('"')[5])
                    self.store.commit_rule(rule, depth)
                    self.store.remember("rule", f"committed a rule: {rule[:80]}")
            except Exception as exc:  # noqa: BLE001 — thread errors are logged, not fatal
                self.store.finish_thread(thread_id, error=str(exc))
                logger.warning("wake question failed: %s", exc)

        self.store.cycle += 1
        self.flush(force=True)
        return new_beliefs

    def _sleep(self):
        """Run one sleep cycle: dream, promote supported dreams, persist."""
        self.dreamer.rng = random.Random()  # nosec B311 - dream RNG, not cryptography
        dreams = self.dreamer.dream(count=3)
        promoted = self.dreamer.promote(dreams)
        self.store.attention = self.window.pairs
        self.flush(force=True)
        return promoted
