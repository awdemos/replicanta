"""Small shared file helpers."""

import os
import re
import tempfile
from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a supplied path escapes the allowed root."""


def slug(name):
    """Filesystem-safe slug for scenario/skill names (shared by mud.py and
    skills.py)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def safe_path(root, subpath):
    """Resolve subpath under root and reject traversal outside root."""
    root = Path(root).resolve()
    target = (root / subpath).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(f"path {subpath!r} escapes root {root}") from exc
    return target


def safe_name(name):
    """Reject names that could be used for path traversal or outside a single directory."""
    if not isinstance(name, str):
        raise UnsafePathError("name must be a string")
    if not name or name in (".", ".."):
        raise UnsafePathError(f"invalid name {name!r}")
    if any(ch in name for ch in ("/", "\\", "\x00")):
        raise UnsafePathError(f"name contains path separator: {name!r}")
    if name.startswith("..") or "/../" in name or name.endswith("/.."):
        raise UnsafePathError(f"name contains parent traversal: {name!r}")
    return name


def atomic_write_text(path, text, root=None):
    """Write text to path atomically: temp file in the same directory,
    then os.replace. A crash mid-write can never leave a half-written
    file — matters for state.json, genomes and skill files, which are
    the organism's only long-term memory.

    If root is supplied, the final resolved path must be inside root;
    otherwise UnsafePathError is raised.
    """
    path = Path(path)
    if root is not None:
        safe_path(root, path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
