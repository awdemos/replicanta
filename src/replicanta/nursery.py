"""Nursery: the organisms' home on disk. Each organism lives in its own
`organisms/<name>/` subdirectory (genome, state, artifacts — its whole
body), a `current` pointer file remembers which one is awake, and the
seed `organism.scl` at the nursery root is the read-only template every
new organism is copied from. Pure filesystem logic — no textual or
organism imports — so it is unit-testable without a terminal."""

import json
import re
import shutil
from pathlib import Path

from replicanta.fileutil import atomic_write_text

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
            f"invalid organism name {name!r} — use letters, digits, - and _"
        )


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


# -- groups ------------------------------------------------------------------

GROUPS_FILE = "groups.json"
GROUP_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _.-]{0,31}$")


def _validate_group(name):
    if not GROUP_NAME_RE.match(name):
        raise ValueError(
            f"invalid group name {name!r} — use letters, digits, spaces, -, _ and ."
        )


def load_groups(root):
    """Group name -> sorted member organism names, read from groups.json.
    Missing or corrupt files read as no groups; members whose organism
    directory no longer exists are pruned. Empty groups survive — a group
    can exist before anything is assigned to it."""
    try:
        raw = json.loads((Path(root) / GROUPS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    existing = set(list_organisms(root))
    groups = {}
    for name, members in raw.items():
        if not GROUP_NAME_RE.match(str(name)):
            continue
        kept = (
            sorted(m for m in members if m in existing)
            if isinstance(members, list)
            else []
        )
        groups[str(name)] = kept
    return groups


def save_groups(root, groups):
    atomic_write_text(
        Path(root) / GROUPS_FILE, json.dumps(groups, indent=2, sort_keys=True) + "\n"
    )


def list_groups(root):
    """Sorted names of every group in the nursery."""
    return sorted(load_groups(root))


def create_group(root, name):
    """Create an empty group. ValueError on an invalid or taken name."""
    _validate_group(name)
    groups = load_groups(root)
    if name in groups:
        raise ValueError(f"group {name!r} already exists")
    groups[name] = []
    save_groups(root, groups)


def rename_group(root, old, new):
    """Rename a group, keeping its members. ValueError on an invalid new
    name, a missing old group, or a taken new name."""
    _validate_group(new)
    groups = load_groups(root)
    if old not in groups:
        raise ValueError(f"no group named {old!r}")
    if new == old:
        return
    if new in groups:
        raise ValueError(f"group {new!r} already exists")
    groups[new] = groups.pop(old)
    save_groups(root, groups)


def remove_group(root, name):
    """Dissolve a group; its members become ungrouped."""
    groups = load_groups(root)
    if name not in groups:
        raise ValueError(f"no group named {name!r}")
    del groups[name]
    save_groups(root, groups)


def group_of(root, org_name):
    """The group an organism belongs to, or None when ungrouped."""
    for name, members in load_groups(root).items():
        if org_name in members:
            return name
    return None


def assign(root, org_name, group_name):
    """Move an organism into a group (None = ungrouped), removing it from
    whatever group it was in. ValueError on an unknown organism or group."""
    if org_name not in list_organisms(root):
        raise ValueError(f"no organism named {org_name!r}")
    groups = load_groups(root)
    if group_name is not None and group_name not in groups:
        raise ValueError(f"no group named {group_name!r}")
    for members in groups.values():
        if org_name in members:
            members.remove(org_name)
    if group_name is not None:
        groups[group_name] = sorted(groups[group_name] + [org_name])
    save_groups(root, groups)
