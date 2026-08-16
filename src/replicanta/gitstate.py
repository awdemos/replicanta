"""Git state probe: sense the repository the organism lives in.

Turns git status into symbolic beliefs and distress, the same way
SystemProbe turns CPU/memory into beliefs and distress.
"""

import logging
import subprocess  # nosec B404 - required for git CLI integration
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = {
    "dirty_many_at": 15,
    "unpushed_many_at": 5,
    "behind_many_at": 20,
}

DEFAULT_WEIGHTS = {
    "dirty_weight": 0.05,
    "dirty_many_weight": 0.08,
    "unpushed_weight": 0.05,
    "unpushed_many_weight": 0.08,
    "behind_weight": 0.05,
    "behind_many_weight": 0.10,
}

DISTRESS_CAP = 0.25

CONDITION_TEXT = {
    "dirty": "worktree has uncommitted changes",
    "dirty_many": "worktree has many uncommitted changes",
    "unpushed": "branch has unpushed commits",
    "unpushed_many": "branch has many unpushed commits",
    "behind": "branch is behind upstream",
    "behind_many": "branch is far behind upstream",
}


def _git_spawn(worktree, args):
    """Run a git command in worktree. Returns a CompletedProcess."""
    return subprocess.run(  # nosec
        ["git", *args],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


class GitProbe:
    """Reads git state from a worktree. All git interaction is injectable via
    `spawn` so tests can supply fake output without touching a real repo."""

    def __init__(self, worktree, config=None, spawn=None):
        self.worktree = Path(worktree)
        self.config = config or {}
        self.spawn = spawn if spawn is not None else _git_spawn
        self._prev_adverse = set()
        self.new_adverse = set()
        self._warning_emitted = False

    # -- configuration helpers -----------------------------------------------
    def _threshold(self, key):
        return self.config.get(key, DEFAULT_THRESHOLDS[key])

    def _weight(self, key):
        return self.config.get(key, DEFAULT_WEIGHTS[key])

    # -- raw snapshot --------------------------------------------------------
    def snapshot(self):
        """Return a git snapshot dict, or {\"is_repo\": False}."""
        if not self._is_repo():
            return {"is_repo": False}
        return {
            "is_repo": True,
            "branch": self._branch(),
            "upstream": self._upstream(),
            "dirty_count": self._dirty_count(),
            "unpushed_count": self._ahead_behind()[0],
            "behind_count": self._ahead_behind()[1],
        }

    def _run(self, args):
        result = self.spawn(self.worktree, args)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout

    def _is_repo(self):
        try:
            out = self._run(["rev-parse", "--is-inside-work-tree"])
        except OSError as exc:
            if not self._warning_emitted:
                logger.warning("git binary unavailable: %s", exc)
                self._warning_emitted = True
            return False
        except RuntimeError:
            return False
        return out.strip() == "true"

    def _branch(self):
        try:
            return self._run(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        except (OSError, RuntimeError):
            return None

    def _upstream(self):
        try:
            return self._run(["rev-parse", "--abbrev-ref", "HEAD@{upstream}"]).strip()
        except (OSError, RuntimeError):
            return None

    def _dirty_count(self):
        try:
            out = self._run(["status", "--porcelain"])
        except (OSError, RuntimeError):
            return 0
        return sum(1 for line in out.splitlines() if line.strip())

    def _ahead_behind(self):
        if self._upstream() is None:
            return (None, None)
        try:
            ahead = int(
                self._run(["rev-list", "--count", "HEAD@{upstream}..HEAD"]).strip()
            )
            behind = int(
                self._run(["rev-list", "--count", "HEAD..HEAD@{upstream}"]).strip()
            )
        except (OSError, RuntimeError, ValueError):
            return (None, None)
        return (ahead, behind)

    # -- quantization --------------------------------------------------------
    def beliefs(self, snap):
        """Turn a snapshot into symbolic (obj, attr, val) beliefs."""
        if not snap["is_repo"]:
            return {}
        b = {}
        b[("git", "dirty", self._level(snap["dirty_count"], "dirty_many_at"))] = 0.9
        unpushed = snap["unpushed_count"]
        if unpushed is None:
            b[("git", "unpushed", "none")] = 0.9
        else:
            b[("git", "unpushed", self._level(unpushed, "unpushed_many_at"))] = 0.9
        behind = snap["behind_count"]
        if behind is None:
            b[("git", "behind", "none")] = 0.9
        else:
            b[("git", "behind", self._level(behind, "behind_many_at"))] = 0.9
        return b

    def _level(self, count, many_key):
        if count == 0:
            return "none"
        if count >= self._threshold(many_key):
            return "many"
        return "few"

    # -- distress ------------------------------------------------------------
    def distress(self, snap):
        """Edge-triggered stress amount. Returns float; stores newly seen
        adverse conditions in `self.new_adverse`."""
        current = self._adverse_conditions(snap)
        self.new_adverse = current - self._prev_adverse
        self._prev_adverse = current
        amount = sum(self._weight(f"{c}_weight") for c in self.new_adverse)
        return min(amount, DISTRESS_CAP)

    def _adverse_conditions(self, snap):
        if not snap["is_repo"]:
            return set()
        conds = set()
        dirty = snap["dirty_count"]
        if dirty:
            conds.add("dirty")
            if dirty >= self._threshold("dirty_many_at"):
                conds.add("dirty_many")
        unpushed = snap["unpushed_count"]
        if unpushed:
            conds.add("unpushed")
            if unpushed >= self._threshold("unpushed_many_at"):
                conds.add("unpushed_many")
        behind = snap["behind_count"]
        if behind:
            conds.add("behind")
            if behind >= self._threshold("behind_many_at"):
                conds.add("behind_many")
        return conds

    # -- summary -------------------------------------------------------------
    def summary(self, snap):
        if not snap["is_repo"]:
            return "not a git repository"
        branch = snap["branch"] or "(unknown)"
        dirty = snap["dirty_count"]
        unpushed = snap["unpushed_count"]
        behind = snap["behind_count"]
        parts = [f"{dirty}△"]
        if unpushed is not None:
            parts.append(f"{unpushed}↑")
        if behind is not None:
            parts.append(f"{behind}↓")
        return f"{branch} · " + " · ".join(parts)
