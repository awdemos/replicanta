"""Atomic file writes: the organism's state, genome and skills are its only
long-term memory — a crash mid-write must leave the old file intact, never
a half-written one."""

import pytest

from replicanta.fileutil import UnsafePathError, atomic_write_text, safe_name, safe_path


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "state.json"
    atomic_write_text(target, '{"ok": true}')
    assert target.read_text() == '{"ok": true}'
    assert list(tmp_path.glob("*.tmp")) == []  # no temp litter


def test_atomic_write_replaces_atomically(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("old, complete")
    # a crash during the write leaves the previous content intact
    import os

    real_replace = os.replace

    def crashing_replace(src, dst):
        raise RuntimeError("power cut")

    monkeypatch.setattr(os, "replace", crashing_replace)
    try:
        atomic_write_text(target, "new, partial")
        raise AssertionError("should have raised")
    except RuntimeError:
        pass
    assert target.read_text() == "old, complete"
    assert list(tmp_path.glob("*.tmp")) == []
    monkeypatch.setattr(os, "replace", real_replace)
    atomic_write_text(target, "new, complete")
    assert target.read_text() == "new, complete"


def test_safe_path_resolves_inside_root(tmp_path):
    subdir = tmp_path / "nested"
    subdir.mkdir()
    assert safe_path(tmp_path, "nested/file.txt") == subdir / "file.txt"


@pytest.mark.parametrize(
    "subpath",
    ["../outside.txt", "nested/../../../outside.txt", "/etc/passwd"],
)
def test_safe_path_rejects_traversal(tmp_path, subpath):
    with pytest.raises(UnsafePathError):
        safe_path(tmp_path, subpath)


def test_safe_name_accepts_plain_names():
    assert safe_name("state.json") == "state.json"


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "a/b", "a\\b", "a\x00b", "../x", "x/../y", "x/.."],
)
def test_safe_name_rejects_dangerous_names(name):
    with pytest.raises(UnsafePathError):
        safe_name(name)


def test_atomic_write_with_root_blocks_traversal(tmp_path):
    target = tmp_path / "safe" / "state.json"
    target.parent.mkdir()
    with pytest.raises(UnsafePathError):
        atomic_write_text("../evil.json", "x", root=tmp_path)
    atomic_write_text(target, "ok", root=tmp_path)
    assert target.read_text() == "ok"
