"""Extension registry (tier B executable skills): the organism may propose
patches to three data-driven behaviors — extra learning patterns, utterance
seeds, sentiment vocabulary — stored in artifacts/extensions.json. Every
entry is validated, nothing applies without explicit approval, and the
registry is versioned so the last applied entry can be reverted.

Consumers (learning, narration, sentiment) read the module-level registry
via active_entries(); it is (re)loaded by load_global() — on organism
startup and after every approve/reject/revert. Pure module: no textual.

When the local arbiter typed-decision server is configured (ARBITER_URL),
applying a patch is preceded by one batched typed decision scoring its
risk (a score over trivial..critical plus a noul that it could break core
functionality). A blocked patch is refused, a medium-or-higher patch
waits for the explicit /approve even when auto-apply is on, and any
arbiter failure falls back to the current apply-what-was-approved
behavior. See _patch_risk."""

import json
import logging
import re
import threading
from pathlib import Path

from replicanta.fileutil import atomic_write_text
from replicanta import typeddecisions

logger = logging.getLogger(__name__)

_EMPTY = {"version": 0, "entries": [], "pending": None}


# -- validation ---------------------------------------------------------------

_CONTROL = ("the weather is nice", "what do you think", "hello there")

KINDS = ("pattern", "seed", "harsh_term", "kind_term")

# Approved patterns run against every chat message on the (single-threaded)
# server, so a pathological regex is a denial of service. Keep them small and
# reject nested quantifiers — `(a+)+`-style ambiguity is the classic
# catastrophic-backtracking shape.
_MAX_PATTERN_LEN = 200
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*{]")


# -- arbiter risk gate ----------------------------------------------------------
#
# One batched typed decision scores a patch before it is applied: a score
# over the ordered risk levels (the answer is the expectation, so the band
# edges sit at the half-integers between levels) plus a noul that the patch
# could break core functionality. "blocked" refuses the patch, "confirm"
# keeps it behind the explicit /approve even when auto-apply is on, "ok"
# applies. None means arbiter is unconfigured or failed — the caller keeps
# its current behavior. The gate runs inside the registry lock; a down
# arbiter only stalls the first mutation (the client cool-down suppresses
# further attempts for DOWNTIME_SECONDS).

_RISK_LEVELS = ["trivial", "low", "medium", "high", "critical"]
_RISK_CONFIRM_AT = 1.5  # expectation at or above the medium band
_RISK_BLOCK_AT = 3.5  # expectation at or above the critical band
_RISK_BREAK_NOUL = 0.8


def _patch_risk(entry):
    """'blocked' | 'confirm' | 'ok', or None when arbiter cannot say."""
    if not typeddecisions.enabled():
        return None
    state = "Proposed self-modification patch for the organism:\n" + json.dumps(entry, sort_keys=True)
    answers = typeddecisions.decide(
        state,
        {
            "risk": typeddecisions.q_score(
                "How risky is applying this self-modification patch to the organism?", _RISK_LEVELS
            ),
            "breaks": typeddecisions.q_noul("This patch could break core functionality of the organism."),
        },
    )
    if answers is None:
        return None
    if (typeddecisions.noul_of(answers, "breaks") or 0.0) >= _RISK_BREAK_NOUL:
        return "blocked"
    score = typeddecisions.score_of(answers, "risk")
    if score is None:
        return None
    if score >= _RISK_BLOCK_AT:
        return "blocked"
    if score >= _RISK_CONFIRM_AT:
        return "confirm"
    return "ok"


def validate(entry):
    """Check a proposed entry. Returns (ok, reason)."""

    kind = entry.get("kind")
    if kind == "pattern":
        pattern = entry.get("regex", "")
        if not (1 <= len(pattern) <= _MAX_PATTERN_LEN):
            return False, f"regex must be 1-{_MAX_PATTERN_LEN} chars"
        if _NESTED_QUANTIFIER.search(pattern):
            return False, "nested quantifiers can backtrack catastrophically"
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return False, "regex does not compile"
        parts = entry.get("template", "").split(":")
        if len(parts) != 3 or not all(parts):
            return False, "template must be obj:attr:value"
        # The substituted belief must survive BeliefStore.add's vocabulary
        # rule (^[a-z_]+$); otherwise approval stages a pattern that kills
        # hear() the first time it fires. Mirror the firing path in
        # learning._extract_facts: substitute the example's capture.
        m = rx.search(entry.get("example", ""))
        if m is not None:
            raw = m.group(1) if m.groups() else m.group(0)
            raw = raw.lower().replace(" ", "_")
            if not all(re.fullmatch(r"[a-z_]+", p.replace("{x}", raw)) for p in parts):
                return False, "template builds a belief outside the vocabulary"
        example = entry.get("example", "")
        if not example or not rx.search(example):
            return False, "does not fire on its own example"
        if any(rx.search(c) for c in _CONTROL):
            return False, "fires on unrelated sentences"
        return True, "ok"
    if kind == "seed":
        text = entry.get("text", "")
        if not (3 <= len(text) <= 60):
            return False, "seed must be 3-60 chars"
        return True, "ok"
    if kind in ("harsh_term", "kind_term"):
        if not re.fullmatch(r"[a-z ]{2,30}", entry.get("text", "")):
            return False, "term must be 2-30 lowercase letters/spaces"
        return True, "ok"
    return False, "unknown kind"


# -- registry io ----------------------------------------------------------------


def _read(path):
    path = Path(path)
    if not path.exists():
        return dict(_EMPTY)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return dict(_EMPTY)  # a corrupt registry reads as empty
    if not isinstance(data, dict):
        return dict(_EMPTY)  # a non-object JSON document reads as empty
    version = data.get("version")
    entries = data.get("entries")
    return {
        "version": version if isinstance(version, int) and not isinstance(version, bool) else _EMPTY["version"],
        "entries": entries if isinstance(entries, list) else [],
        "pending": data.get("pending"),
    }


def _write(path, registry):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(registry, indent=2))


# -- process-global module-level registry -------------------------------------
#
# The registry consumers read must be visible on every thread: the web
# server and the TUI's reflection worker run on their own threads, and a
# thread-local registry left the pending-mutation panel empty and approved
# extensions unfired. One process-global default registry, guarded by a
# lock; every mutation holds the lock for the whole read-modify-write so
# concurrent propose/approve pairs cannot lose each other's updates.


class ExtensionRegistry:
    """Process-global (or per-instance) validated extension registry.

    The module-level default is shared by all threads; each mutation
    (propose/approve/reject/revert/reload) holds ``_lock`` across the whole
    read-modify-write."""

    def __init__(self):
        self._data = None
        self._lock = threading.RLock()

    def load_global(self, path):
        """(Re)load this registry from ``path``."""
        with self._lock:
            self._data = _read(path)

    def reset(self):
        """Forget the in-memory registry state."""
        with self._lock:
            self._data = None

    def registry(self):
        """Return the loaded registry dict, or the empty default."""
        with self._lock:
            return self._data if self._data is not None else dict(_EMPTY)

    def active_entries(self, kind):
        return [e for e in self.registry()["entries"] if e.get("kind") == kind]

    def pending(self):
        return self.registry().get("pending")

    def entries(self):
        return self.registry()["entries"]

    def propose(self, path, entry, auto_apply=False):
        """Stage an entry as pending, or apply it immediately if auto_apply is True.

        Returns the applied entry when auto_apply=True, otherwise None.
        With arbiter configured, auto-apply still stages the patch when the
        risk gate says it needs confirmation, and leaves a blocked patch
        pending (visible, rejectable, and re-gated by approve).
        """
        ok, reason = validate(entry)
        if not ok:
            raise ValueError(f"invalid extension: {reason}")
        with self._lock:
            reg = _read(path)
            reg["pending"] = entry
            _write(path, reg)
            self.load_global(path)
            if auto_apply:
                risk = _patch_risk(entry)
                if risk == "confirm":
                    logger.info(
                        "patch (%s) scored medium+ risk; staged for /approve instead of auto-applying",
                        entry.get("kind"),
                    )
                    return None
                if risk == "blocked":
                    logger.warning(
                        "patch (%s) scored critical risk; left pending — /approve will refuse it",
                        entry.get("kind"),
                    )
                    return None
                return self.approve(path)
        return None

    def approve(self, path):
        """Apply the pending entry: append to entries, bump version, reload.

        Returns the applied entry, or None when nothing is pending, the
        pending entry fails validation, or the arbiter risk gate blocks it
        (a blocked patch is cleared — refused). Invalid pending entries are
        cleared.
        """
        with self._lock:
            reg = _read(path)
            entry = reg.get("pending")
            if entry is None:
                return None
            ok, _reason = validate(entry)
            if not ok:
                reg["pending"] = None
                _write(path, reg)
                self.load_global(path)
                return None
            if _patch_risk(entry) == "blocked":
                reg["pending"] = None
                _write(path, reg)
                self.load_global(path)
                logger.warning("patch (%s) blocked by risk gate; refusing to apply", entry.get("kind"))
                return None
            reg["entries"].append(entry)
            reg["pending"] = None
            reg["version"] += 1
            _write(path, reg)
            self.load_global(path)
            return entry

    def reject(self, path):
        """Discard the pending entry. Returns it, or None."""
        with self._lock:
            reg = _read(path)
            entry = reg.get("pending")
            reg["pending"] = None
            _write(path, reg)
            self.load_global(path)
            return entry

    def revert_last(self, path):
        """Remove the most recently applied entry. Returns it, or None."""
        with self._lock:
            reg = _read(path)
            if not reg["entries"]:
                return None
            entry = reg["entries"].pop()
            reg["version"] += 1
            _write(path, reg)
            self.load_global(path)
            return entry


_REGISTRY = ExtensionRegistry()


def _default_registry():
    return _REGISTRY


def load_global(path):
    """(Re)load the module-level registry consumers read."""
    _default_registry().load_global(path)


def reset():
    """Forget the module-level registry (test isolation)."""
    _default_registry().reset()


def registry():
    return _default_registry().registry()


def active_entries(kind):
    return _default_registry().active_entries(kind)


def pending():
    return _default_registry().pending()


def entries():
    return _default_registry().entries()


def propose(path, entry, auto_apply=False):
    return _default_registry().propose(path, entry, auto_apply=auto_apply)


def approve(path):
    return _default_registry().approve(path)


def reject(path):
    return _default_registry().reject(path)


def revert_last(path):
    return _default_registry().revert_last(path)
