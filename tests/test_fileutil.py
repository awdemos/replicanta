"""Atomic file writes: the organism's state, genome and skills are its only
long-term memory — a crash mid-write must leave the old file intact, never
a half-written one."""

from replicanta.fileutil import atomic_write_text


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
