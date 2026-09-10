import subprocess

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
