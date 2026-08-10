"""Small shared file helpers."""

import os
import re
import tempfile
from pathlib import Path


def slug(name):
    """Filesystem-safe slug for scenario/skill names (shared by mud.py and
    skills.py)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def atomic_write_text(path, text):
    """Write text to path atomically: temp file in the same directory,
    then os.replace. A crash mid-write can never leave a half-written
    file — matters for state.json, genomes and skill files, which are
    the organism's only long-term memory."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
