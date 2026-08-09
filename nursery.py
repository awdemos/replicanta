"""Nursery: the organisms' home on disk. Each organism lives in its own
`organisms/<name>/` subdirectory (genome, state, artifacts — its whole
body), a `current` pointer file remembers which one is awake, and the
seed `organism.scl` at the nursery root is the read-only template every
new organism is copied from. Pure filesystem logic — no textual or
organism imports — so it is unit-testable without a terminal."""

import re
import shutil
from pathlib import Path

from fileutil import atomic_write_text

NURSERY_DIR = "organisms"
CURRENT_FILE = "current"
DEFAULT_NAME = "default"
AUTO_NAME_PREFIX = "replicanta"

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _nursery(root):
    return Path(root) / NURSERY_DIR


def _validate(name):
    if not NAME_RE.match(name):
        raise ValueError(
            f"invalid organism name {name!r} — use letters, "
            "digits, - and _")


def list_organisms(root):
    """Sorted names of every organism in the nursery."""
    nursery = _nursery(root)
    if not nursery.is_dir():
        return []
    return sorted(p.name for p in nursery.iterdir() if p.is_dir())


def organism_dir(root, name):
    return _nursery(root) / name


def create(root, name, template_scl):
    """Birth a new organism: its own directory seeded with a copy of the
    template genome. ValueError on an invalid or taken name."""
    _validate(name)
    dest = organism_dir(root, name)
    if dest.exists():
        raise ValueError(f"organism {name!r} already exists")
    dest.mkdir(parents=True)
    shutil.copy(template_scl, dest / "organism.scl")
    return dest


def next_name(root):
    """First free auto-name (replicanta-2, replicanta-3, …) for bare /new."""
    taken = set(list_organisms(root))
    n = 2
    while f"{AUTO_NAME_PREFIX}-{n}" in taken:
        n += 1
    return f"{AUTO_NAME_PREFIX}-{n}"


def rename(root, old, new):
    """Rename an organism: move its whole directory and repoint `current`
    when the renamed one is the awake organism. Names may mix upper- and
    lowercase; on case-insensitive filesystems a change of letter case
    alone goes through a temporary name so the move cannot clobber the
    source. ValueError on an invalid or taken new name, or when the old
    organism does not exist. Returns the new directory. Callers holding
    a live Organism must flush it before the move and reopen it from the
    new path afterwards."""
    _validate(new)
    src = organism_dir(root, old)
    if not src.is_dir():
        raise ValueError(f"no organism named {old!r}")
    if new == old:
        return src
    dest = organism_dir(root, new)
    if new.lower() == old.lower():
        # pure case change: two hops so case-insensitive filesystems
        # (and filesystems where src == dest) survive the move
        tmp = organism_dir(root, f"{old}.rename-tmp")
        src.rename(tmp)
        tmp.rename(dest)
    else:
        if dest.exists():
            raise ValueError(f"organism {new!r} already exists")
        src.rename(dest)
    if current(root) == old:
        set_current(root, new)
    return dest


def current(root):
    """The active organism's name ('default' when never set)."""
    pointer = Path(root) / CURRENT_FILE
    try:
        name = pointer.read_text().strip()
    except OSError:
        return DEFAULT_NAME
    return name or DEFAULT_NAME


def set_current(root, name):
    atomic_write_text(Path(root) / CURRENT_FILE, name + "\n")


def migrate(root):
    """Move a legacy root-level organism (state.json + artifacts/ living
    next to the seed genome) into organisms/default/. Its evolved
    organism.scl is copied along; the root copy stays as the template.
    No-op when there is nothing to migrate or default already exists."""
    root = Path(root)
    state = root / "state.json"
    dest = organism_dir(root, DEFAULT_NAME)
    if not state.exists() or dest.exists():
        return False
    dest.mkdir(parents=True)
    shutil.move(str(state), dest / "state.json")
    artifacts = root / "artifacts"
    if artifacts.is_dir():
        shutil.move(str(artifacts), dest / "artifacts")
    scl = root / "organism.scl"
    if scl.exists():
        shutil.copy(scl, dest / "organism.scl")
    set_current(root, DEFAULT_NAME)
    return True
