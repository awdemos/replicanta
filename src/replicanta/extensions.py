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
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return dict(_EMPTY)  # a corrupt registry reads as empty


def _write(path, registry):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(registry, indent=2))


# -- thread-local module-level registry ---------------------------------------
#
# Global mutable state leaks between organisms and tests. Each thread gets its
# own default ExtensionRegistry; module-level helpers delegate to the current
# thread's default.

class ExtensionRegistry:
    """Per-thread (or per-instance) validated extension registry."""

    def __init__(self):
        self._data = None

    def load_global(self, path):
        """(Re)load this registry from ``path``."""
        self._data = _read(path)

    def reset(self):
        """Forget the in-memory registry state."""
        self._data = None

    def registry(self):
        """Return the loaded registry dict, or the empty default."""
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
        reg = _read(path)
        entry = reg.get("pending")
        reg["pending"] = None
        _write(path, reg)
        self.load_global(path)
        return entry

    def revert_last(self, path):
        """Remove the most recently applied entry. Returns it, or None."""
        reg = _read(path)
        if not reg["entries"]:
            return None
        entry = reg["entries"].pop()
        reg["version"] += 1
        _write(path, reg)
        self.load_global(path)
        return entry


_REGISTRY_LOCAL = threading.local()


def _default_registry() -> ExtensionRegistry:
    try:
        return _REGISTRY_LOCAL.registry
    except AttributeError:
        reg = ExtensionRegistry()
        _REGISTRY_LOCAL.registry = reg
        return reg


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
