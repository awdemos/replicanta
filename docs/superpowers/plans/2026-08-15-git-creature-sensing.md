# Git-Creature Sensing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add git-state sensing to organisms so they perceive dirty worktrees, unpushed commits, and commits behind upstream as beliefs, stress, and memories.

**Architecture:** A new `GitProbe` module mirrors `SystemProbe`; a new `config` module loads/saves `replicanta.toml`; the `Organism` optionally holds a `GitProbe` and folds its output into the existing sense/stress/memory pipeline; three TUI commands expose the feature.

**Tech Stack:** Python 3.14, `tomllib`, `subprocess`, `pytest`, existing `replicanta` codebase.

---

## File map

| File | Responsibility |
|------|----------------|
| `src/replicanta/config.py` | Load/save `replicanta.toml`; provide defaults. |
| `src/replicanta/gitstate.py` | `GitProbe`: snapshot, beliefs, distress, summary for a git worktree. |
| `src/replicanta/organism.py` | Wire `GitProbe` into `sense()`; add `git_enable/disable/status`. |
| `src/replicanta/tui_commands.py` | Register `/git` command. |
| `src/replicanta/tui.py` | Dispatch `/git on|off|status` to the organism. |
| `tests/test_config.py` | Config load/save behaviour. |
| `tests/test_gitstate.py` | GitProbe snapshot/beliefs/distress/summary. |
| `tests/test_organism.py` | Organism integration with fake probes. |
| `tests/test_tui_commands.py` | Command registration and help text. |

---

### Task 1: Create `src/replicanta/config.py`

**Files:**
- Create: `src/replicanta/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the config module**

```python
"""Project configuration loader for replicanta.toml."""

import logging
from pathlib import Path

import tomllib

from replicanta.fileutil import atomic_write_text

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "git": {
        "enabled": False,
        "dirty_many_at": 15,
        "unpushed_many_at": 5,
        "behind_many_at": 20,
        "dirty_weight": 0.05,
        "dirty_many_weight": 0.08,
        "unpushed_weight": 0.05,
        "unpushed_many_weight": 0.08,
        "behind_weight": 0.05,
        "behind_many_weight": 0.10,
    }
}


def config_path(root):
    return Path(root) / "replicanta.toml"


def load_config(root):
    """Load replicanta.toml from root, merging user values over defaults.
    Missing or malformed files fall back to defaults (a warning is logged
    for malformed files)."""
    path = config_path(root)
    if not path.is_file():
        return _copy(DEFAULT_CONFIG)
    try:
        with path.open("rb") as f:
            user = tomllib.load(f)
    except Exception as exc:  # noqa: BLE001 — config errors must not crash boot
        logger.warning("cannot read %s: %s; using defaults", path, exc)
        return _copy(DEFAULT_CONFIG)
    merged = _copy(DEFAULT_CONFIG)
    _merge_table(merged, user)
    return merged


def save_config(root, config):
    """Write config back to replicanta.toml. Only primitive values in
    top-level tables are supported (bool, int, float, str)."""
    atomic_write_text(config_path(root), _render_config(config))


def _copy(cfg):
    return {k: dict(v) if isinstance(v, dict) else v for k, v in cfg.items()}


def _merge_table(default, user):
    for key, default_val in default.items():
        if isinstance(default_val, dict):
            user_val = user.get(key, {})
            if isinstance(user_val, dict):
                default[key] = {**default_val, **user_val}
        elif key in user:
            default[key] = user[key]


def _render_config(config):
    lines = []
    for section, values in config.items():
        lines.append(f"[{section}]")
        for key, val in values.items():
            if isinstance(val, bool):
                lines.append(f"{key} = {'true' if val else 'false'}")
            elif isinstance(val, str):
                lines.append(f'{key} = "{val}"')
            else:
                lines.append(f"{key} = {val}")
        lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_config.py`:

```python
from pathlib import Path

import pytest

from replicanta import config


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = config.load_config(tmp_path)
    assert cfg["git"]["enabled"] is False
    assert cfg["git"]["dirty_many_at"] == 15
    assert cfg["git"]["behind_many_weight"] == 0.10


def test_load_config_reads_user_values(tmp_path):
    (tmp_path / "replicanta.toml").write_text(
        '[git]\nenabled = true\ndirty_many_at = 99\n'
    )
    cfg = config.load_config(tmp_path)
    assert cfg["git"]["enabled"] is True
    assert cfg["git"]["dirty_many_at"] == 99
    assert cfg["git"]["unpushed_many_at"] == 5  # default preserved


def test_load_config_malformed_file_returns_defaults(tmp_path, caplog):
    (tmp_path / "replicanta.toml").write_text("[git\nenabled = true\n")
    with caplog.at_level("WARNING"):
        cfg = config.load_config(tmp_path)
    assert cfg["git"]["enabled"] is False
    assert "cannot read" in caplog.text


def test_save_config_roundtrip(tmp_path):
    cfg = config.load_config(tmp_path)
    cfg["git"]["enabled"] = True
    cfg["git"]["dirty_many_at"] = 42
    config.save_config(tmp_path, cfg)
    loaded = config.load_config(tmp_path)
    assert loaded["git"]["enabled"] is True
    assert loaded["git"]["dirty_many_at"] == 42


def test_save_config_preserves_unrelated_section(tmp_path):
    (tmp_path / "replicanta.toml").write_text('[voice]\nmodel = "alan"\n')
    cfg = config.load_config(tmp_path)
    cfg["git"]["enabled"] = True
    config.save_config(tmp_path, cfg)
    loaded = config.load_config(tmp_path)
    assert loaded["voice"]["model"] == "alan"
    assert loaded["git"]["enabled"] is True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`

Expected: `ModuleNotFoundError: No module named 'replicanta.config'` (or import errors for the test file).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/replicanta/config.py tests/test_config.py
git commit -m "feat: add replicanta.toml config loader"
```

---

### Task 2: Create `src/replicanta/gitstate.py`

**Files:**
- Create: `src/replicanta/gitstate.py`
- Test: `tests/test_gitstate.py`

- [ ] **Step 1: Write the GitProbe module**

```python
"""Git state probe: sense the repository the organism lives in.

Turns git status into symbolic beliefs and distress, the same way
SystemProbe turns CPU/memory into beliefs and distress.
"""

import logging
import subprocess
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
    return subprocess.run(
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
        up = f" {unpushed}↑" if unpushed is not None else ""
        dn = f" {behind}↓" if behind is not None else ""
        return f"{branch} · {dirty}△{up}{dn}"
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_gitstate.py`:

```python
import subprocess
from pathlib import Path

import pytest

from replicanta.gitstate import DEFAULT_THRESHOLDS, GitProbe


def _spawn(responses):
    """responses maps 'arg string' -> (returncode, stdout, stderr)."""
    def spawn(_worktree, args):
        cmd = " ".join(args)
        rc, out, err = responses.get(cmd, (1, "", f"unexpected: {cmd}"))
        return subprocess.CompletedProcess(args, rc, out, err)
    return spawn


def _repo_responses(branch="main", upstream="origin/main", dirty=0, ahead=0, behind=0):
    return {
        "rev-parse --is-inside-work-tree": (0, "true\n", ""),
        "rev-parse --abbrev-ref HEAD": (0, f"{branch}\n", ""),
        "rev-parse --abbrev-ref HEAD@{upstream}": (0, f"{upstream}\n", ""),
        "status --porcelain": (0, "M file.py\n" * dirty, ""),
        "rev-list --count HEAD@{upstream}..HEAD": (0, f"{ahead}\n", ""),
        "rev-list --count HEAD..HEAD@{upstream}": (0, f"{behind}\n", ""),
    }


def test_not_a_git_repo():
    probe = GitProbe(
        "/tmp",
        spawn=_spawn({"rev-parse --is-inside-work-tree": (1, "", "not a git repo")}),
    )
    snap = probe.snapshot()
    assert snap["is_repo"] is False
    assert probe.beliefs(snap) == {}
    assert probe.distress(snap) == 0.0


def test_clean_repo():
    probe = GitProbe("/tmp", spawn=_spawn(_repo_responses()))
    snap = probe.snapshot()
    assert snap["dirty_count"] == 0
    assert snap["unpushed_count"] == 0
    assert snap["behind_count"] == 0
    b = probe.beliefs(snap)
    assert b[("git", "dirty", "none")] == pytest.approx(0.9)
    assert b[("git", "unpushed", "none")] == pytest.approx(0.9)
    assert b[("git", "behind", "none")] == pytest.approx(0.9)


def test_dirty_repo():
    probe = GitProbe("/tmp", spawn=_spawn(_repo_responses(dirty=3, ahead=2)))
    snap = probe.snapshot()
    assert snap["dirty_count"] == 3
    b = probe.beliefs(snap)
    assert b[("git", "dirty", "few")] == pytest.approx(0.9)
    assert b[("git", "unpushed", "few")] == pytest.approx(0.9)


def test_dirty_many():
    probe = GitProbe(
        "/tmp",
        spawn=_spawn(_repo_responses(dirty=DEFAULT_THRESHOLDS["dirty_many_at"])),
    )
    snap = probe.snapshot()
    b = probe.beliefs(snap)
    assert b[("git", "dirty", "many")] == pytest.approx(0.9)


def test_unpushed_many():
    probe = GitProbe(
        "/tmp",
        spawn=_spawn(_repo_responses(ahead=DEFAULT_THRESHOLDS["unpushed_many_at"])),
    )
    snap = probe.snapshot()
    b = probe.beliefs(snap)
    assert b[("git", "unpushed", "many")] == pytest.approx(0.9)


def test_behind_many():
    probe = GitProbe(
        "/tmp",
        spawn=_spawn(_repo_responses(behind=DEFAULT_THRESHOLDS["behind_many_at"])),
    )
    snap = probe.snapshot()
    b = probe.beliefs(snap)
    assert b[("git", "behind", "many")] == pytest.approx(0.9)


def test_no_upstream():
    responses = _repo_responses(ahead=0, behind=0)
    responses["rev-parse --abbrev-ref HEAD@{upstream}"] = (128, "", "no upstream")
    probe = GitProbe("/tmp", spawn=_spawn(responses))
    snap = probe.snapshot()
    assert snap["upstream"] is None
    assert snap["unpushed_count"] is None
    assert snap["behind_count"] is None
    b = probe.beliefs(snap)
    assert b[("git", "unpushed", "none")] == pytest.approx(0.9)
    assert b[("git", "behind", "none")] == pytest.approx(0.9)


def test_distress_edge_triggered():
    probe = GitProbe("/tmp", spawn=_spawn(_repo_responses(dirty=3)))
    snap = probe.snapshot()
    first = probe.distress(snap)
    assert first == pytest.approx(0.05)
    assert "dirty" in probe.new_adverse
    for _ in range(5):
        assert probe.distress(probe.snapshot()) == 0.0
        assert probe.new_adverse == set()
    # clear dirty
    probe.spawn = _spawn(_repo_responses())
    probe.distress(probe.snapshot())
    # dirty again
    probe.spawn = _spawn(_repo_responses(dirty=3))
    assert probe.distress(probe.snapshot()) == pytest.approx(0.05)


def test_summary():
    probe = GitProbe(
        "/tmp", spawn=_spawn(_repo_responses(dirty=3, ahead=2, behind=1))
    )
    snap = probe.snapshot()
    assert probe.summary(snap) == "main · 3△ · 2↑ · 1↓"


def test_summary_not_a_repo():
    probe = GitProbe(
        "/tmp",
        spawn=_spawn({"rev-parse --is-inside-work-tree": (1, "", "nope")}),
    )
    assert probe.summary(probe.snapshot()) == "not a git repository"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_gitstate.py -v`

Expected: `ModuleNotFoundError: No module named 'replicanta.gitstate'`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_gitstate.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/replicanta/gitstate.py tests/test_gitstate.py
git commit -m "feat: add GitProbe for dirty/unpushed/behind sensing"
```

---

### Task 3: Wire GitProbe into `Organism`

**Files:**
- Modify: `src/replicanta/organism.py`
- Test: `tests/test_organism.py`

- [ ] **Step 1: Add imports and modify `__init__`**

Change the top imports in `src/replicanta/organism.py` from:

```python
from replicanta import extensions, goals, learning, mud, sentiment
```

to:

```python
from replicanta import config as project_config
from replicanta import extensions, goals, learning, mud, sentiment
from replicanta.gitstate import GitProbe, CONDITION_TEXT as GIT_CONDITION_TEXT
```

Change the `__init__` signature from:

```python
def __init__(
    self, dir_path, wake_seconds=180, sleep_seconds=60, chaos=0.5, probe=None
):
```

to:

```python
def __init__(
    self,
    dir_path,
    wake_seconds=180,
    sleep_seconds=60,
    chaos=0.5,
    probe=None,
    git_probe=None,
):
```

Add these two lines before `self._since_sense = ...`:

```python
self.git_probe = git_probe
self._git_warning_emitted = False
```

- [ ] **Step 2: Attach probe during `load()`**

Add this block at the end of `load()`, after `self.window.refresh(cycle=self.store.cycle)`:

```python
        cfg = project_config.load_config(self._root_dir())
        if cfg.get("git", {}).get("enabled"):
            self._attach_git_probe(cfg.get("git", {}))
```

- [ ] **Step 3: Add helper methods to `Organism`**

Add these methods to `src/replicanta/organism.py` (place them near `sense()` or `load()`):

```python
    def _root_dir(self):
        """Project root: grandparent of an organism in organisms/; otherwise
        the organism's own directory."""
        if self.dir_path.parent.name == "organisms":
            return self.dir_path.parent.parent
        return self.dir_path

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
```

- [ ] **Step 4: Extend `sense()` to fold git state**

Change `sense()` in `src/replicanta/organism.py` from:

```python
    def sense(self):
        """Perceive the host machine: fold a fresh metric snapshot into the
        belief store and let adverse conditions raise stress. Returns the
        distress amount applied (0 when the host is fine). Persistence is
        the caller's job (`flush()`), so sensing stays cheap to schedule."""
        snap = self.probe.snapshot()
        for belief, conf in self.probe.beliefs(snap).items():
            self.store.observe(belief, conf)
        distress = self.probe.distress(snap)
        if distress:
            self.meter.bump(distress)
        return distress
```

to:

```python
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
```

- [ ] **Step 5: Write the organism integration tests**

Update the imports at the top of `tests/test_organism.py` to:

```python
import shutil
import subprocess
from pathlib import Path

from replicanta.organism import Mind, Organism
from replicanta.probe import SystemProbe
```

Add these module-level helpers near the existing `SCL` constant:

```python
def _dummy_probe():
    return SystemProbe(proc=Path("/nonexistent"), sys=Path("/nonexistent"))


def _seed_organism(tmp_path):
    shutil.copy(SCL, tmp_path / "organism.scl")


def test_organism_git_probe_disabled_by_default(tmp_path):
    _seed_organism(tmp_path)
    org = Organism(tmp_path, probe=_dummy_probe())
    org.load()
    assert org.git_probe is None


def test_organism_git_probe_attached_when_enabled(tmp_path):
    _seed_organism(tmp_path)
    (tmp_path / "replicanta.toml").write_text("[git]\nenabled = true\n")
    org = Organism(tmp_path, probe=_dummy_probe())
    org.load()
    assert org.git_probe is not None


def test_organism_sense_folds_git_beliefs(tmp_path, monkeypatch):
    _seed_organism(tmp_path)
    from replicanta.gitstate import GitProbe

    fake = GitProbe(
        tmp_path,
        spawn=lambda _w, _a: subprocess.CompletedProcess(_a, 0, "true\n", ""),
    )
    monkeypatch.setattr(
        fake,
        "snapshot",
        lambda: {
            "is_repo": True,
            "branch": "main",
            "upstream": "origin/main",
            "dirty_count": 3,
            "unpushed_count": 2,
            "behind_count": 0,
        },
    )
    org = Organism(tmp_path, probe=_dummy_probe(), git_probe=fake)
    org.load()
    org.sense()
    assert ("git", "dirty", "few") in org.store.beliefs()
    assert ("git", "unpushed", "few") in org.store.beliefs()
    assert ("git", "behind", "none") in org.store.beliefs()


def test_organism_git_distress_records_memory(tmp_path, monkeypatch):
    _seed_organism(tmp_path)
    from replicanta.gitstate import GitProbe

    fake = GitProbe(
        tmp_path,
        spawn=lambda _w, _a: subprocess.CompletedProcess(_a, 0, "true\n", ""),
    )
    monkeypatch.setattr(
        fake,
        "snapshot",
        lambda: {
            "is_repo": True,
            "branch": "main",
            "upstream": "origin/main",
            "dirty_count": 3,
            "unpushed_count": 0,
            "behind_count": 0,
        },
    )
    org = Organism(tmp_path, probe=_dummy_probe(), git_probe=fake)
    org.load()
    org.sense()
    assert any("uncommitted" in m.get("text", "") for m in org.store.memory)


def test_organism_git_enable_disable_persist_config(tmp_path):
    _seed_organism(tmp_path)
    from replicanta import config

    org = Organism(tmp_path, probe=_dummy_probe())
    org.load()
    org.git_enable()
    assert org.git_probe is not None
    assert config.load_config(tmp_path)["git"]["enabled"] is True
    org.git_disable()
    assert org.git_probe is None
    assert config.load_config(tmp_path)["git"]["enabled"] is False


def test_organism_git_status(tmp_path, monkeypatch):
    _seed_organism(tmp_path)
    from replicanta.gitstate import GitProbe

    fake = GitProbe(
        tmp_path,
        spawn=lambda _w, _a: subprocess.CompletedProcess(_a, 0, "true\n", ""),
    )
    monkeypatch.setattr(
        fake,
        "snapshot",
        lambda: {
            "is_repo": True,
            "branch": "main",
            "upstream": "origin/main",
            "dirty_count": 3,
            "unpushed_count": 2,
            "behind_count": 1,
        },
    )
    org = Organism(tmp_path, probe=_dummy_probe(), git_probe=fake)
    org.load()
    assert org.git_status() == "main · 3△ · 2↑ · 1↓"


def test_organism_git_status_when_disabled(tmp_path):
    _seed_organism(tmp_path)
    org = Organism(tmp_path, probe=_dummy_probe())
    org.load()
    assert org.git_status() == "git sensing is off"
```

- [ ] **Step 6: Run organism tests**

Run: `pytest tests/test_organism.py -v`

Expected: all tests pass, including the new ones.

- [ ] **Step 7: Commit**

```bash
git add src/replicanta/organism.py tests/test_organism.py
git commit -m "feat: wire GitProbe into Organism sense/stress/memory pipeline"
```

---

### Task 4: Add `/git` TUI commands

**Files:**
- Modify: `src/replicanta/tui_commands.py`
- Modify: `src/replicanta/tui.py`
- Test: `tests/test_tui_commands.py`

- [ ] **Step 1: Register `/git` in the command list**

In `src/replicanta/tui_commands.py`, add this entry to `COMMANDS` (near the other slash commands):

```python
    ("/git", "/git [on|off|status]", "toggle or show git sensing"),
```

- [ ] **Step 2: Add help text snippet**

Append this to the `help_text()` return value, after the existing scripting section:

```python
        "",
        "git sensing: /git on|off toggles whether the organism feels the",
        "worktree state; /git status shows the current repo summary.",
```

- [ ] **Step 3: Dispatch `/git` in the TUI**

In `src/replicanta/tui.py`, add this branch inside `_dispatch()` before the final `else`:

```python
        elif name == "/git":
            self._git_command(parts[1:])
```

Add the new method `_git_command` to the `ReplicantaApp` class:

```python
    def _git_command(self, args):
        if not args or args[0] == "status":
            self._append_log(self.org.git_status(), STYLE_DIM)
        elif args[0] == "on":
            self.org.git_enable()
            self._append_log("git sensing on", STYLE_DIM)
        elif args[0] == "off":
            self.org.git_disable()
            self._append_log("git sensing off", STYLE_DIM)
        else:
            self._append_log("/git [on|off|status]", STYLE_DIM)
```

- [ ] **Step 4: Write the command tests**

Add to `tests/test_tui_commands.py`:

```python
def test_git_command_registered():
    assert "/git" in COMMAND_NAMES


def test_help_text_includes_git():
    text = help_text()
    assert "/git" in text
    assert "git sensing" in text
```

- [ ] **Step 5: Run tests and lint**

Run: `pytest tests/test_tui_commands.py -v`

Expected: all tests pass.

Run: `ruff check src/replicanta/tui_commands.py src/replicanta/tui.py tests/test_tui_commands.py`

Expected: no issues.

- [ ] **Step 6: Commit**

```bash
git add src/replicanta/tui_commands.py src/replicanta/tui.py tests/test_tui_commands.py
git commit -m "feat: add /git on|off|status TUI commands"
```

---

### Task 5: Final verification

**Files:** all of the above.

- [ ] **Step 1: Run the full test suite**

Run: `pytest tests/ -q`

Expected: all tests pass.

- [ ] **Step 2: Run the linter**

Run: `ruff check src/replicanta tests`

Expected: no issues.

- [ ] **Step 3: Smoke-test in the TUI (optional)**

Start replicanta in a git repo:

```bash
uv run replicanta
```

Type:

```
/git status
/git on
/git status
```

Expected: first `/git status` says "git sensing is off"; after `/git on`, `/git status` prints the repo summary like `main · 0△ · 0↑ · 0↓`.

- [ ] **Step 4: Commit any final fixes**

If any fixes were needed:

```bash
git add -A
git commit -m "fix: address review/lint issues for git sensing"
```

---

## Self-review

**Spec coverage:**
- Config file with defaults/overrides → Task 1.
- `GitProbe` snapshot/beliefs/distress/summary → Task 2.
- Organism integration (sense, stress, memory) → Task 3.
- TUI commands `/git on|off|status` → Task 4.
- Tests for config, gitstate, organism, TUI → all tasks.

**Placeholder scan:** No TBD/TODO/fill-in placeholders. Each step includes exact file paths, code, and commands.

**Type consistency:**
- `GitProbe.__init__` signature `(worktree, config=None, spawn=None)` used consistently.
- `Organism.__init__` gains `git_probe=None` and stores `self.git_probe`.
- `CONDITION_TEXT` imported as `GIT_CONDITION_TEXT` in organism.py to avoid naming clash.
