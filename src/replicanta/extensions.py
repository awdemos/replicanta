"""Extension registry (tier B executable skills): the organism may propose
patches to three data-driven behaviors — extra learning patterns, utterance
seeds, sentiment vocabulary — stored in artifacts/extensions.json. Every
entry is validated, nothing applies without explicit approval, and the
registry is versioned so the last applied entry can be reverted.

Consumers (learning, narration, sentiment) read the module-level registry
via active_entries(); it is (re)loaded by load_global() — on organism
startup and after every approve/reject/revert. Pure module: no textual."""

import json
import re
import threading
from pathlib import Path

from replicanta.fileutil import atomic_write_text

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
                return self.approve(path)
        return None

    def approve(self, path):
        """Apply the pending entry: append to entries, bump version, reload.

        Returns the applied entry, or None when nothing is pending or the
        pending entry fails validation. Invalid pending entries are cleared.
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
