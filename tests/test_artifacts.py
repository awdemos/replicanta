"""Artifacts feature: the organism keeps a diary on disk — a body of work
outside the chat. narration.diary_entry voices entries (fallback offline),
Organism.write_diary persists them, tick() emits want_diary on cadence."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import llmclient
import voice
from organism import Organism
from probe import SystemProbe
from conftest import patch_generate


def _organism(tmp_path, **kwargs):
    kwargs.setdefault(
        "probe", SystemProbe(proc="/nonexistent/proc", sys="/nonexistent/sys"))
    org = Organism(tmp_path, **kwargs)
    org.load()
    return org


def test_tick_emits_want_diary_on_cadence(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.store.cycle = Organism.DIARY_INTERVAL
    kinds = [e["kind"] for e in org.tick(1.0)]
    assert "want_diary" in kinds
    # stamped immediately: no repeat on the next tick
    kinds = [e["kind"] for e in org.tick(1.0)]
    assert "want_diary" not in kinds


def test_tick_no_want_diary_before_cadence(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.store.cycle = 2
    kinds = [e["kind"] for e in org.tick(1.0)]
    assert "want_diary" not in kinds


def test_write_diary_appends_entry_with_header(tmp_path):
    org = _organism(tmp_path)
    org.store.cycle = 7
    org.write_diary("today I learned about rain.")
    diary = tmp_path / "artifacts" / "diary.md"
    text = diary.read_text()
    assert "cycle 7" in text
    assert "today I learned about rain." in text
    org.write_diary("a second entry.")
    assert diary.read_text().count("cycle 7") == 2


def test_write_diary_remembers_episode(tmp_path):
    org = _organism(tmp_path)
    org.write_diary("quiet days.")
    assert any(m["kind"] == "diary" for m in org.store.memory)


def test_diary_entry_prompt_branch(tmp_path, monkeypatch):
    org = _organism(tmp_path)
    captured = {}
    patch_generate(monkeypatch, lambda prompt, *a, **k: captured.setdefault("p", prompt) or "x")
    voice.diary_entry(org)
    assert "diary entry" in captured["p"]


def test_diary_entry_fallback_offline(tmp_path):
    org = _organism(tmp_path)
    llmclient._voice.online = False
    entry = voice.diary_entry(org)
    assert entry
    assert "cycle" in entry or "mood" in entry
