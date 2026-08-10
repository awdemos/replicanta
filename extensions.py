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
from pathlib import Path

from fileutil import atomic_write_text

_EMPTY = {"version": 0, "entries": [], "pending": None}

_REGISTRY = None


# -- validation ---------------------------------------------------------------

_CONTROL = ("the weather is nice", "what do you think", "hello there")

KINDS = ("pattern", "seed", "harsh_term", "kind_term")


def validate(entry):
    """Check a proposed entry. Returns (ok, reason)."""
    kind = entry.get("kind")
    if kind == "pattern":
        try:
            rx = re.compile(entry.get("regex", ""), re.IGNORECASE)
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


def load_global(path):
    """(Re)load the module-level registry consumers read."""
    global _REGISTRY
    _REGISTRY = _read(path)


def reset():
    """Forget the module-level registry (test isolation)."""
    global _REGISTRY
    _REGISTRY = None


def registry():
    return _REGISTRY if _REGISTRY is not None else dict(_EMPTY)


def active_entries(kind):
    return [e for e in registry()["entries"] if e.get("kind") == kind]


def pending():
    return registry().get("pending")


# -- proposal lifecycle -----------------------------------------------------------

def propose(path, entry):
    """Stage an entry as pending (replaces any previous pending one)."""
    reg = _read(path)
    reg["pending"] = entry
    _write(path, reg)
    load_global(path)


def approve(path):
    """Apply the pending entry: append to entries, bump version, reload.
    Returns the applied entry, or None when nothing is pending."""
    reg = _read(path)
    entry = reg.get("pending")
    if entry is None:
        return None
    reg["entries"].append(entry)
    reg["pending"] = None
    reg["version"] += 1
    _write(path, reg)
    load_global(path)
    return entry


def reject(path):
    """Discard the pending entry. Returns it, or None."""
    reg = _read(path)
    entry = reg.get("pending")
    reg["pending"] = None
    _write(path, reg)
    load_global(path)
    return entry


def revert_last(path):
    """Remove the most recently applied entry. Returns it, or None."""
    reg = _read(path)
    if not reg["entries"]:
        return None
    entry = reg["entries"].pop()
    reg["version"] += 1
    _write(path, reg)
    load_global(path)
    return entry
