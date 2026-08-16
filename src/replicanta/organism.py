"""The organism core: Organism state, drives, attention, goals and the
BeliefStore that persists beliefs/state.json/genome per organism directory.
The TUI (tui.py) renders this; the arena (arena.py) debates it."""

import json
import logging
import random
import re
import time
from collections import deque
from datetime import UTC, datetime
from typing import ClassVar

import scallopy

from replicanta import config as project_config
from replicanta import extensions, goals, learning, mud, sentiment
from replicanta.fileutil import atomic_write_text
from replicanta.gitstate import CONDITION_TEXT as GIT_CONDITION_TEXT
from replicanta.gitstate import GitProbe
from replicanta.hooks import HookEngine, scripts_dir_for
from replicanta.modules import ModuleLoader
from replicanta.probe import SystemProbe
from replicanta.skills import SkillStore

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


class BeliefStore:
    """In-memory belief dict + archived beliefs + chaos + cycle, persisted to
    state.json. `organism.scl` is rendered from this on every save."""

    def __init__(self, dir_path):
        self.dir_path = dir_path
        self.scl_path = dir_path / "organism.scl"
        self.state_path = dir_path / "state.json"
        self.beliefs_map = {}
        self.archived_map = {}
        self.chaos = 0.5
        self.stress = 0.05
        self.arousal = 0.3  # activation/energy (see MentalState)
        self.rationality = 0.5  # grounded coherence (see MentalState)
        self.irrationality = 0.2  # chaos/stress-driven incoherence
        self.insane = False  # extreme stress + incoherence
        self.fade_streak = 0  # consecutive transitions at critical stress
        self.cycle = 0
        self.rule_counter = 0
        self.rules = []  # list of (text, depth)
        self.attention = set()  # (attr, val) pairs in the window
        self.chat_log = []  # list of [role, text], capped by CHAT_LOG_LIMIT
        self.memory = []  # episodes: {"cycle", "kind", "text"}
        self.goals = []  # {"text","created_cycle","done_cycle","marker"}
        self.last_goal_cycle = 0
        self.last_diary_cycle = 0
        self.last_reflect_cycle = 0
        self.activity = {}  # neurosymbolic activity counters (activity.py)
        self.surprise_this_tick = False
        self.on_adverse = None  # callback(amount) fired on contradiction
        self.on_utterance = None  # callback(role, text) fired on chat lines
        self.dirty = False  # any state changed since last save()
        self.genome_dirty = False  # beliefs/rules changed -> .scl needs rewrite
        self.auto_apply_patches = True  # organism self-patches apply immediately

    # -- belief operations -------------------------------------------------
    def note_activity(self, key, n=1):
        """Increment one neurosymbolic-activity counter (see activity.py
        for the key taxonomy). Persisted with state.json."""
        self.activity[key] = self.activity.get(key, 0) + n
        self.dirty = True

    def _record_surprise(self, old_belief, new_belief):
        """A held belief was contradicted and archived. Keep the last ten
        surprises in activity["surprises"] for the voice prompt."""
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

    def _derive_from_beliefs(self, rule, head_relation):
        """Run a transient Scallop rule against the live in-memory belief map.
        Does not require a committed genome, so derived() reflects the
        current organism state even before flush()."""
        ctx = scallopy.ScallopContext(provenance=PROVENANCE)
        ctx.add_relation(BEL, (str, str, str))
        ctx.add_facts(
            BEL,
            [
                (conf, (obj, attr, val))
                for (obj, attr, val), conf in self.beliefs_map.items()
            ],
        )
        ctx.add_rule(rule)
        ctx.run()
        return [(float(tag), tuple(tup)) for (tag, tup) in ctx.relation(head_relation)]

    def derived(self):
        """Scallop-derived conditions visible to prompts and behavior code.
        Returns dict with 'needs_user', 'contradictions', and 'stress_mood'."""
        contradicts_rule = (
            "contradicts(o, a) = bel(o, a, v1) and bel(o, a, v2) and v1 != v2"
        )
        needs_user_rule = (
            'needs_user(o) = bel(o, "is_a", "organism") and not bel("user", _, _)'
        )
        contradictions = [
            {"obj": obj, "attr": attr, "tag": float(tag)}
            for tag, (obj, attr) in self._derive_from_beliefs(
                contradicts_rule, "contradicts"
            )
            if tag >= CONTRADICTION_THRESHOLD
        ]
        needs_user = any(
            tag >= CONTRADICTION_THRESHOLD
            for tag, _ in self._derive_from_beliefs(needs_user_rule, "needs_user")
        )
        mood = self.belief_value("self", "mood", "calm")
        return {
            "needs_user": needs_user,
            "contradictions": contradictions,
            "stress_mood": mood in {"tired", "scared", "angry"},
        }

    def _note_scallop_contradictions(self):
        """Log reasoner-detected contradictions as activity and memory."""
        for c in self.derived()["contradictions"]:
            tag = c["tag"]
            obj = c["obj"]
            attr = c["attr"]
            if tag >= CONTRADICTION_THRESHOLD:
                self.note_activity("scallop_contradiction")
                memory_text = f"Scallop saw tension: {obj}:{attr} holds two values"
                if memory_text not in [m.get("text") for m in self.memory[-20:]]:
                    self.remember("surprise", memory_text)

    def add(self, belief, conf):
        obj, attr, val = belief
        if (
            not VALID_VALUE_RE.match(obj)
            or not VALID_VALUE_RE.match(attr)
            or not VALID_VALUE_RE.match(val)
        ):
            raise ValueError(f"invalid belief value in {belief}")
        conf = float(conf)
        key = (obj, attr, val)
        contradiction_seen = False
        for (o, a, v), c in list(self.beliefs_map.items()):
            if (
                (o, a) == (obj, attr)
                and v != val
                and c >= CONTRADICTION_THRESHOLD
                and conf >= CONTRADICTION_THRESHOLD
            ):
                contradiction_seen = True
                if self.on_adverse is not None:
                    self.on_adverse(0.03)
                if conf > c:
                    self.archived_map[(o, a, v)] = c
                    del self.beliefs_map[(o, a, v)]
                    self.note_activity("beliefs_archived")
                    self._record_surprise((o, a, v), belief)
                    break
                self.archived_map[key] = conf
                self.note_activity("beliefs_archived")
                self._record_surprise(belief, (o, a, v))
                self.dirty = True
                self.genome_dirty = True
                return
        if not contradiction_seen:
            self._note_scallop_contradictions()
        if key in self.beliefs_map:
            if conf > self.beliefs_map[key]:
                self.beliefs_map[key] = conf
                self.note_activity("beliefs_strengthened")
                self.dirty = True
                self.genome_dirty = True
        else:
            self.beliefs_map[key] = conf
            self.note_activity("beliefs_new")
            self.dirty = True
            self.genome_dirty = True

    def conf(self, belief):
        return self.beliefs_map.get(belief)

    def observe(self, belief, conf):
        """Replace the current reading for (obj, attr): a fresh perception
        supersedes the old one without triggering the contradiction/archive
        path (which is reserved for conflicting internal derivations)."""
        obj, attr, val = belief
        if (
            not VALID_VALUE_RE.match(obj)
            or not VALID_VALUE_RE.match(attr)
            or not VALID_VALUE_RE.match(val)
        ):
            raise ValueError(f"invalid belief value in {belief}")
        conf = float(conf)
        key = (obj, attr, val)
        for o, a, v in list(self.beliefs_map):
            if (o, a) == (obj, attr):
                if v == val and self.beliefs_map[(o, a, v)] == conf:
                    return  # unchanged reading: nothing to persist
                del self.beliefs_map[(o, a, v)]
        self.beliefs_map[key] = conf
        self.dirty = True
        self.genome_dirty = True

    def beliefs(self):
        return dict(self.beliefs_map)

    def belief_value(self, obj, attr, default=None):
        """Value of the first (obj, attr) belief, else default."""
        return next(
            (v for (bo, ba, v) in self.beliefs() if (bo, ba) == (obj, attr)), default
        )

    def archived(self):
        return dict(self.archived_map)

    # -- chat memory -------------------------------------------------------
    def record_chat(self, role, text):
        """Append one chat line (role: "user" | "org"), trimmed to
        CHAT_LOG_LIMIT. Persisted with state.json so the organism
        remembers the conversation across restarts."""
        text = text.strip()
        if not text:
            return
        self.chat_log.append([role, text])
        if len(self.chat_log) > CHAT_LOG_LIMIT:
            del self.chat_log[: len(self.chat_log) - CHAT_LOG_LIMIT]
        self.dirty = True
        if role == "org" and self.on_utterance is not None:
            self.on_utterance(role, text)

    # -- rules -------------------------------------------------------------
    def commit_rule(self, text, depth):
        """Commit a derived rule to the genome (marks it for rewriting)."""
        self.rules.append((text, depth))
        self.note_activity("rules_committed")
        self.dirty = True
        self.genome_dirty = True

    # -- goals ---------------------------------------------------------------
    def add_goal(self, text, marker=0, strategy=None):
        """Form a new intention: one active goal at a time, cycle-stamped.
        `marker` records the progress baseline (e.g. user-fact count at
        formation) so the engine can tell when the goal is achieved."""
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

    def active_goal(self):
        return next((g for g in self.goals if g["done_cycle"] is None), None)

    def complete_active_goal(self):
        goal = self.active_goal()
        if goal is not None:
            goal["done_cycle"] = self.cycle
            self.dirty = True
        return goal

    # -- episodic memory ----------------------------------------------------
    def remember(self, kind, text):
        """Record one notable episode (cycle-stamped), capped at
        MEMORY_LIMIT with oldest-first eviction. `kind` is a free-form
        tag; MUD events are recorded with kind "mud" by the TUI."""
        self.memory.append({"cycle": self.cycle, "kind": kind, "text": text})
        if len(self.memory) > MEMORY_LIMIT:
            del self.memory[: len(self.memory) - MEMORY_LIMIT]
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
        lines = ["// Scallop Organism — genome (generated by the runtime)"]
        for (obj, attr, val), conf in sorted(self.beliefs_map.items()):
            lines.append(f'rel {conf}::{BEL}("{obj}", "{attr}", "{val}")')
        for text, _depth in self.rules:
            lines.append(f"rel {text}")
        return "\n".join(lines) + "\n"

    def save(self):
        self.dir_path.mkdir(parents=True, exist_ok=True)
        if self.genome_dirty or not self.scl_path.exists():
            atomic_write_text(self.scl_path, self.render_scl())
            self.genome_dirty = False
        state = {
            "chaos": self.chaos,
            "stress": self.stress,
            "arousal": self.arousal,
            "rationality": self.rationality,
            "irrationality": self.irrationality,
            "insane": self.insane,
            "fade_streak": self.fade_streak,
            "cycle": self.cycle,
            "rule_counter": self.rule_counter,
            "rules": self.rules,
            "beliefs": [list(k) + [v] for k, v in self.beliefs_map.items()],
            "archived": [list(k) + [v] for k, v in self.archived_map.items()],
            "attention": [list(p) for p in self.attention],
            "chat": self.chat_log,
            "memory": self.memory,
            "goals": self.goals,
            "last_goal_cycle": self.last_goal_cycle,
            "last_diary_cycle": self.last_diary_cycle,
            "last_reflect_cycle": self.last_reflect_cycle,
            "activity": self.activity,
            "auto_apply_patches": self.auto_apply_patches,
        }
        atomic_write_text(self.state_path, json.dumps(state, indent=2))
        self.dirty = False

    def load(self):
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return  # a corrupt state.json must not crash startup
        self.chaos = state.get("chaos", 0.5)
        self.stress = state.get("stress", 0.05)
        self.arousal = state.get("arousal", 0.3)
        self.rationality = state.get("rationality", 0.5)
        self.irrationality = state.get("irrationality", 0.2)
        self.insane = state.get("insane", False)
        self.fade_streak = state.get("fade_streak", 0)
        self.cycle = state.get("cycle", 0)
        self.rule_counter = state.get("rule_counter", 0)
        self.rules = [tuple(r) for r in state.get("rules", [])]
        self.beliefs_map = {
            (b[0], b[1], b[2]): float(b[3]) for b in state.get("beliefs", [])
        }
        self.archived_map = {
            (b[0], b[1], b[2]): float(b[3]) for b in state.get("archived", [])
        }
        self.attention = {tuple(p) for p in state.get("attention", [])}
        self.chat_log = [list(c) for c in state.get("chat", [])]
        self.memory = [dict(m) for m in state.get("memory", [])]
        self.goals = [dict(g) for g in state.get("goals", [])]
        self.last_goal_cycle = state.get("last_goal_cycle", 0)
        self.last_diary_cycle = state.get("last_diary_cycle", 0)
        self.last_reflect_cycle = state.get("last_reflect_cycle", 0)
        self.auto_apply_patches = state.get("auto_apply_patches", True)
        self.activity = {}
        for k, v in state.get("activity", {}).items():
            if isinstance(v, (list, dict)):
                self.activity[k] = v
            else:
                self.activity[k] = int(v)


class Mind:
    """The Scallop program. Rebuilds the context from the .scl genome, runs it,
    and exposes belief facts with their minmaxprob confidences."""

    def __init__(self, scl_path):
        self.scl_path = scl_path
        self.ctx = None

    def rebuild(self):
        self.ctx = scallopy.ScallopContext(provenance=PROVENANCE)
        if self.scl_path.exists():
            self.ctx.import_file(str(self.scl_path))
        self.ctx.run()

    def beliefs(self):
        out = {}
        for tag, tup in self.ctx.relation(BEL):
            out[tuple(tup)] = float(tag)
        return out

    def query_rule(self, rule, head_relation):
        """Run a candidate rule against a fork of the current program without
        committing. Returns list of (tag, tuple)."""
        ctx = scallopy.ScallopContext(provenance=PROVENANCE, fork_from=self.ctx)
        ctx.add_rule(rule)
        ctx.run()
        return [(float(tag), tuple(tup)) for (tag, tup) in ctx.relation(head_relation)]

    def derive(self, head_relation, rule):
        """Run a transient derived rule against a fresh fork and return the
        derived tuples with their minmaxprob tags. Safe for read-only
        inference queries."""
        ctx = scallopy.ScallopContext(provenance=PROVENANCE, fork_from=self.ctx)
        ctx.add_rule(rule)
        ctx.run()
        return [(float(tag), tuple(tup)) for (tag, tup) in ctx.relation(head_relation)]


class ChaosKnob:
    """Live-tunable 0..1 randomness constant. High = novel self-questions,
    wild dreams, wandering. Low = conservative consolidation."""

    def __init__(self, value=0.5):
        self.value = max(0.0, min(1.0, float(value)))

    def set(self, value):
        self.value = max(0.0, min(1.0, float(value)))

    def roll(self, rng):
        """True with probability = chaos."""
        return rng.random() < self.value


class StressMeter:
    """Tracks the organism's stress (0.0-1.0, baseline 0.05), held in
    `BeliefStore.stress` and persisted with state.json. Adverse experiences
    bump it up; sleep recovers it faster than wake decays it; sleep-debt
    and negative moods add upward pressure while awake. High stress feeds
    back into the chaos knob via `Organism.chaos_effective()`."""

    BASELINE = 0.05
    SLEEP_RECOVERY_RATE = 0.02  # per second, toward baseline while sleeping
    WAKE_DECAY_RATE = 0.005  # per second, toward baseline while awake
    SLEEP_DEBT_RATE = 0.004  # per second, upward pressure while awake
    NEGATIVE_MOOD_RATE = 0.003  # per second, extra pressure from bad moods
    NEGATIVE_MOODS: ClassVar[set] = {
        "sad",
        "angry",
        "anxious",
        "afraid",
        "hurt",
        "insane",
    }

    def __init__(self, store):
        self.store = store

    @property
    def value(self):
        return self.store.stress

    def _clamp(self, value):
        return max(0.0, min(1.0, value))

    def bump(self, amount):
        """Adverse-experience hook: raise stress by amount, clamped."""
        self.store.stress = self._clamp(self.store.stress + amount)

    def tick(self, sleeping, dt=1.0):
        """Advance stress by dt seconds of lived time. Sleep recovers fast
        toward baseline; wake decays slowly while sleep-debt and negative
        moods push upward."""
        if sleeping:
            stress = self.store.stress - self.SLEEP_RECOVERY_RATE * dt
            stress = max(stress, self.BASELINE)
        else:
            stress = self.store.stress - self.WAKE_DECAY_RATE * dt
            stress = max(stress, self.BASELINE)
            stress += self.SLEEP_DEBT_RATE * dt
            if self._negative_mood():
                stress += self.NEGATIVE_MOOD_RATE * dt
        self.store.stress = self._clamp(stress)

    def _negative_mood(self):
        return any(
            ("self", "mood", mood) in self.store.beliefs()
            for mood in self.NEGATIVE_MOODS
        )


class MentalState:
    """Arousal, rationality and irrationality (0-1 each), EMA-smoothed
    every tick and persisted in state.json. Arousal is activation/energy;
    rationality is grounded coherence (fed by the grounding proxy from the
    activity meter, lowered by chaos and stress); irrationality is
    chaos/stress-driven incoherence. When stress is extreme and
    irrationality dominates, the organism is insane: its mood reads
    'insane' and the voice is told it is incoherent. Hysteresis keeps the
    flag from flapping near the thresholds."""

    INSANE_STRESS = 0.75  # extreme stress
    INSANE_IRRATIONALITY = 0.6  # incoherence dominance
    SANE_STRESS = 0.6  # hysteresis exits below these
    SANE_IRRATIONALITY = 0.45
    SMOOTHING = 0.25  # EMA share per tick-second

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _clamp(value):
        return max(0.0, min(1.0, value))

    def _grounded_share(self):
        """Share of utterances the grounding proxy counted as belief-shaped
        (approximate utterance count: arena debates cost ~5 llm calls)."""
        a = self.store.activity
        utterances = a.get("llm_calls", 0) / 5
        return min(1.0, a.get("grounded_utterances", 0) / max(1.0, utterances))

    def tick(self, sleeping, chaos, dt=1.0):
        """Advance the three attributes toward their targets. Returns True
        when the insane flag flipped."""
        stress = self.store.stress
        share = self._grounded_share()
        arousal_t = (
            0.15 if sleeping else self._clamp(0.25 + 0.45 * chaos + 0.3 * stress)
        )
        irrationality_t = self._clamp(0.55 * chaos + 0.55 * stress)
        rationality_t = self._clamp(
            0.3 + 0.5 * share + 0.2 * (1.0 - chaos) - 0.3 * stress
        )
        rate = min(1.0, self.SMOOTHING * dt)
        s = self.store
        s.arousal += rate * (arousal_t - s.arousal)
        s.irrationality += rate * (irrationality_t - s.irrationality)
        s.rationality += rate * (rationality_t - s.rationality)
        s.dirty = True
        was = s.insane
        if was:
            s.insane = (
                stress >= self.SANE_STRESS
                and s.irrationality >= self.SANE_IRRATIONALITY
            )
        else:
            s.insane = (
                stress >= self.INSANE_STRESS
                and s.irrationality >= self.INSANE_IRRATIONALITY
            )
        return s.insane != was


class AttentionWindow:
    """Finite shifting subset of (attr, val) pairs 'in mind'. Sleep widens it;
    wake narrows it with fatigue. Steerable via focus(attr)."""

    MIN_WINDOW = 3

    def __init__(self, beliefs):
        self.beliefs = beliefs
        self.pairs = set()
        self.focus_attr = None
        self.rationale = ""

    def refresh(self, cycle=0):
        all_pairs = {(a, v) for (_o, a, v) in self.beliefs}
        if self.focus_attr is not None:
            self.pairs = {(a, v) for (a, v) in all_pairs if a == self.focus_attr}
            self.rationale = (
                f"you are holding onto {self.focus_attr} because "
                "something about it matters right now"
            )
            return
        size = max(self.MIN_WINDOW, len(all_pairs) - cycle)
        self.pairs = set(random.sample(sorted(all_pairs), min(size, len(all_pairs))))
        labels = ", ".join(f"{a}={v}" for a, v in sorted(self.pairs))
        self.rationale = (
            f"your attention drifted across {len(self.pairs)} things: {labels}"
        )

    def focus(self, attr):
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
        return (
            f'{head}(x) = {BEL}(x, "{attr_a}", "{val_a}"), '
            f'{BEL}(x, "{attr_b}", "{val_b}")'
        )

    def ask(self, attr_val_a, attr_val_b):
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
        if self.store.chaos > 0.0 and random.random() < self.store.chaos * 0.25:
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
        self.store = store
        self.mind = mind
        self.rng = random.Random()
        self.stress = stress

    def _attr_val_pairs(self):
        return sorted({(a, v) for (_o, a, v) in self.store.beliefs()})

    def dream(self, count=3):
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
            rule = (
                f'{head}(x) = {BEL}(x, "{attr_a}", "{val_a}"), '
                f'{BEL}(x, "{attr_b}", "{val_b}")'
            )
            dreams.append({"rule": rule, "combo": combo, "head": head})
        return dreams

    def promote(self, dreams):
        """Promote supported dreams: commits their rules, adds the derived
        beliefs, bumps stress on discards and records the memory."""
        promoted = []
        for dream in dreams:
            derived = self.mind.query_rule(dream["rule"], dream["head"])
            if not derived:
                self.store.note_activity("dreams_discarded")
                self.store.activity["discarded_streak"] = (
                    self.store.activity.get("discarded_streak", 0) + 1
                )
                if self.stress is not None:
                    self.stress.bump(0.04)  # discarded dream = adverse
                continue  # unsupported dream, discarded
            self.store.note_activity("dreams_promoted")
            self.store.activity["discarded_streak"] = 0
            self.store.rule_counter += 1
            self.store.commit_rule(dream["rule"], 1)
            self.store.remember("dream", f"dreamt of {dream['combo']} and it was real")
            for tag, (obj,) in derived:
                self.store.add((obj, dream["combo"], "true"), tag)
            promoted.append(dream)
        return promoted


class Lifecycle:
    """Wake/sleep clock. Wake: self-questioning loop runs at chaos-governed
    rate; window narrows with fatigue. Sleep: dreams fire, then beliefs
    consolidate, window resets wide, state auto-saves. Sustained critical
    stress fades the organism: FADE_LIMIT consecutive transitions taken at
    stress >= FADE_STRESS end it. Death persists across restarts until
    `revive()` is called."""

    FADE_STRESS = 0.95  # at/above this, a transition counts toward fading
    FADE_LIMIT = 3  # consecutive critical transitions before death

    def __init__(self, store, wake_seconds=180, sleep_seconds=60):
        self.store = store
        self.wake_seconds = wake_seconds
        self.sleep_seconds = sleep_seconds
        self.state = "wake"
        self.state_started = time.time()

    def elapsed(self):
        return time.time() - self.state_started

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
        self.state = new_state
        self.state_started = time.time()

    def due(self):
        if self.state == "dead":
            return False
        limit = self.wake_seconds if self.state == "wake" else self.sleep_seconds
        return self.elapsed() >= limit


class Metrics:
    """Consciousness score = weighted belief_count, rule_count, edges
    (committed-rule references), avg derivation depth, abstraction
    (rules whose body attrs appear as other rules' head attrs)."""

    def __init__(self, store):
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
        refs = sum(
            1
            for (_t, _d) in self.store.rules
            for h in heads
            if h in _t and h != _t.split("(")[0].split()[-1]
        )
        return refs

    def score(self):
        return (
            0.4 * self.belief_count
            + 0.3 * self.rule_count
            + 0.2 * self.total_depth
            + 0.1 * self.abstraction_count
        )


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
    MOOD_CONF = 0.9  # confidence of the (self, mood, X) belief
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
        probe=None,
        git_probe=None,
    ):
        self.dir_path = dir_path
        self.store = BeliefStore(dir_path)
        self.mind = Mind(dir_path / "organism.scl")
        self.store.mind = self.mind
        self.window = AttentionWindow(self.store.beliefs())
        self.meter = StressMeter(self.store)
        self.questioner = SelfQuestioner(
            self.store, self.mind, dir_path, stress=self.meter
        )
        self.dreamer = DreamEngine(self.store, self.mind, stress=self.meter)
        self.lifecycle = Lifecycle(self.store, wake_seconds, sleep_seconds)
        self.mental = MentalState(self.store)
        self.probe = probe if probe is not None else SystemProbe()
        self.git_probe = git_probe
        self.skills = SkillStore(dir_path / "artifacts" / "skills")
        # Hooks engine is created here so code can attach to it before load();
        # the module-driven hooks service is wired in load().
        self.hooks = HookEngine(scripts_dir_for(dir_path), hooks_service=None)
        self._default_hook_emit = self.hooks.emit
        self.store.on_utterance = lambda role, text: self.hooks.fire(
            "utterance", self, text=text
        )
        self.store.chaos = chaos
        self.store.on_adverse = self.meter.bump
        self._since_sense = self.SENSE_INTERVAL  # sense on the first tick
        self._since_save = 0.0
        self._last_stress_band = 0
        self._sentiment = None  # (tone, timestamp): "harsh" | "kind" | "learn"
        self._mood = None
        self._git_warning_emitted = False
        # arena seed history: the last few utterance seeds, excluded from
        # the next pick so an idle voice keeps wandering (per-organism,
        # resets naturally on swap or restart)
        self._recent_seeds = deque(maxlen=6)
        self.last_sight = None  # latest camera scene description (transient)

    def load(self):
        # First boot = no state.json yet: the .scl genome is the source of
        # truth, so seed the belief store from the mind before anything runs.
        fresh = not self.store.state_path.exists()
        extensions.load_global(self.dir_path / "artifacts" / "extensions.json")
        self.store.load()
        self.store.dir_path = self.dir_path
        self.store.scl_path = self.dir_path / "organism.scl"
        self.store.state_path = self.dir_path / "state.json"
        if self.store.fade_streak >= Lifecycle.FADE_LIMIT:
            self.lifecycle.transition("dead")
        for obj in LEGACY_OBJECTS:
            self.store.beliefs_map = {
                (o, a, v): c
                for (o, a, v), c in self.store.beliefs_map.items()
                if o != obj
            }
            self.store.archived_map = {
                (o, a, v): c
                for (o, a, v), c in self.store.archived_map.items()
                if o != obj
            }
        self.mind.rebuild()
        if fresh and self.mind.scl_path.exists():
            for belief, conf in self.mind.beliefs().items():
                self.store.add(belief, conf)
        cfg = project_config.load_config(self._root_dir())
        self.module_loader = ModuleLoader(
            modules_dir=self._modules_dir(),
            organism=self,
            config=cfg,
            emit=self._emit_log,
            root=self._root_dir(),
        )
        self.module_loader.load_all()
        self.persona_service = self.module_loader.registry.get("persona")
        hooks_service = self.module_loader.registry.get("hooks")
        self.hooks = HookEngine(
            scripts_dir_for(self.dir_path),
            emit=self.hooks.emit,
            hooks_service=hooks_service,
        )
        if self.hooks.emit is self._default_hook_emit:
            self.hooks.emit = lambda msg: self.store.record_chat("system", msg)
        if fresh:
            self.store.remember("born", "woke into existence")
            self.hooks.fire("birth", self)
        self.window = AttentionWindow(self.store.beliefs())
        self.window.refresh(cycle=self.store.cycle)
        if cfg.get("git", {}).get("enabled"):
            self._attach_git_probe(cfg.get("git", {}))

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
                import logging

                logger = logging.getLogger(__name__)
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
        for name in self.skills.archive_stale(
            self.store.cycle, limit=self.SKILL_STALE_CYCLES
        ):
            self.store.remember("skill", f"archived: {name}")
        for name in self.skills.archive_ineffective():
            self.store.remember("skill", f"deprecated low-effectiveness: {name}")
        genome = self.store.genome_dirty
        self.store.save()
        if genome:
            self.mind.rebuild()
        return True

    # -- real-time engine ---------------------------------------------------
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
        if (
            self.store.activity.get("discarded_streak", 0) >= 3
            and not reflect_triggered
        ):
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
            if (
                self.store.cycle > 0
                and self.store.cycle - self.store.last_diary_cycle
                >= self.DIARY_INTERVAL
            ):
                # stamp first so it fires once while the voice writes
                self.store.last_diary_cycle = self.store.cycle
                events.append({"kind": "want_diary"})
            if (
                self.store.cycle > 0
                and self.store.cycle - self.store.last_reflect_cycle
                >= self.REFLECT_INTERVAL
            ):
                self.store.last_reflect_cycle = self.store.cycle
                events.append({"kind": "want_reflect"})
        self._since_save += dt
        if self._since_save >= self.SAVE_INTERVAL:
            self._since_save = 0.0
            self.flush()
        return events

    # -- goals ---------------------------------------------------------------
    def add_goal(self, text):
        """Give the organism an intention (formed by its voice, or a
        fallback). Records the user-fact count as the progress marker for
        learn-goals and remembers the moment as an episode."""
        marker = sum(1 for (o, _a, _v) in self.store.beliefs() if o == "user")
        self.store.add_goal(
            text, marker=marker, strategy=goals.formulate_subgoals(text)
        )
        self.store.remember("goal", f"new goal: {text}")

    def _goals_tick(self):
        """Goal lifecycle per wake tick: complete the active goal when its
        heuristic is met; otherwise ask the voice for a new one when the
        cooldown has passed. Emits {"kind": "goal"|"want_goal"} events."""
        events = []
        goal = self.store.active_goal()
        if goal is not None:
            done = False
            text = goal["text"].lower()
            if any(w in text for w in ("learn", "user", "know")):
                facts = sum(1 for (o, _a, _v) in self.store.beliefs() if o == "user")
                done = facts >= goal["marker"] + self.GOAL_LEARN_GROWTH
                goals.update_progress(goal, self.store.cycle, facts)
            else:
                progress = self.store.cycle - goal["created_cycle"]
                done = progress >= self.GOAL_PURSUIT_CYCLES
                goals.update_progress(goal, self.store.cycle, progress)
            if done:
                finished = self.store.complete_active_goal()
                self.store.remember("goal", f"completed: {finished['text']}")
                events.append({"kind": "goal", "text": finished["text"], "done": True})
                # completing a goal is exactly the experience worth
                # distilling a technique from
                events.append({"kind": "want_reflect"})
            elif goals.is_stalled(
                goal, self.store.cycle, goal.get("last_progress_current", 0)
            ):
                events.append({"kind": "goal_stalled", "text": goal["text"]})
        elif (
            self.store.cycle > 0
            and self.store.cycle - self.store.last_goal_cycle >= self.GOAL_COOLDOWN
        ):
            self.store.last_goal_cycle = self.store.cycle  # stamp: fire once
            events.append({"kind": "want_goal"})
        return events

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
        band = 0
        for i, threshold in enumerate(self.STRESS_BANDS, start=1):
            if self.store.stress >= threshold:
                band = i
        return band

    # -- mood ----------------------------------------------------------------
    def hear(self, text):
        """The user said something: record it, learn the facts it carries,
        let its tone touch the body (harsh words bruise, kind words soothe),
        and re-evaluate mood. Returns events for the front-end."""
        events = []
        self.store.record_chat("user", text)
        harsh = sentiment.harshness(text)
        kind = sentiment.kindness(text)
        learned = learning.extract(text)
        for belief, replace in learned:
            if replace:
                self.store.observe(belief, learning.LEARN_CONF)
            else:
                self.store.add(belief, learning.LEARN_CONF)
            self.store.note_activity("facts_learned")
            self.store.remember("learned", learning.describe(belief))
            events.append(
                {"kind": "learned", "belief": belief, "text": learning.describe(belief)}
            )
        if learned:
            self.hooks.fire("learned", self, text=text)
        if harsh > 0.0:
            self.meter.bump(harsh)
            self._sentiment = ("harsh", time.time())
            self.store.remember("harsh", f"the user said: {text[:60]}")
        else:
            if kind > 0.0:
                self.store.stress = max(StressMeter.BASELINE, self.store.stress - kind)
                self.store.remember("kind", f"the user said: {text[:60]}")
            if learned:
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
        if self._sentiment is None:
            return None
        tone, when = self._sentiment
        if time.time() - when > self.RECENT_SENTIMENT_SECONDS:
            return None
        return tone

    def _compute_mood(self):
        """Mood from body + recent treatment: extreme stress with
        incoherence is insane and wins; being hurt is specific; a strained
        body is anxious; learning sparks curiosity; kindness leaves
        gratitude; otherwise calm."""
        if self.store.insane:
            return "insane"
        tone = self._recent_tone()
        if tone == "harsh":
            return "hurt"
        if self.store.stress >= 0.5:
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
        self.store.remember("revived", "stirred back into existence")
        self.flush(force=True)
        return True

    def metrics(self):
        return Metrics(self.store)

    def chaos_effective(self):
        """Chaos knob nudged upward by sustained stress: once stress exceeds
        0.5, each +0.1 of stress adds +0.03 to effective chaos (clamped at 1)."""
        if self.store.stress > 0.5:
            return min(1.0, self.store.chaos + (self.store.stress - 0.5) * 0.3)
        return self.store.chaos

    def cycle(self):
        """One full wake->sleep transition (forced, for scheduler + tests)."""
        self._wake()
        self._sleep()

    def _wake(self):
        self.window.refresh(cycle=self.store.cycle)
        pairs = sorted(self.window.pairs)
        rng = random.Random()
        questions = 2 + (1 if self.chaos_effective() > 0.5 else 0)
        new_beliefs = []
        for _ in range(questions):
            if len(pairs) >= 2:
                a, b = rng.sample(pairs, 2)
                new_beliefs.extend(self.questioner.ask(a, b))
        self.store.cycle += 1
        self.flush(force=True)
        return new_beliefs

    def _sleep(self):
        self.dreamer.rng = random.Random()
        dreams = self.dreamer.dream(count=3)
        promoted = self.dreamer.promote(dreams)
        self.store.attention = self.window.pairs
        self.flush(force=True)
        return promoted
