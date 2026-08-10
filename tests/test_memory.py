"""Episodic-memory feature: the organism remembers notable episodes
(birth, lessons, dreams, harsh/kind moments, fading, revival), capped and
persisted, and its inner voice can draw on them for continuity."""

from replicanta.narration import build_prompt, state_snapshot
from replicanta.organism import MEMORY_LIMIT, BeliefStore, Lifecycle, Organism
from replicanta.probe import SystemProbe


def _organism(tmp_path, **kwargs):
    kwargs.setdefault(
        "probe", SystemProbe(proc="/nonexistent/proc", sys="/nonexistent/sys")
    )
    org = Organism(tmp_path, **kwargs)
    org.load()
    return org


# -- store mechanics -----------------------------------------------------------


def test_remember_records_cycle_stamped_episode(tmp_path):
    store = BeliefStore(tmp_path)
    store.cycle = 7
    store.remember("learned", "you like rain")
    assert store.memory == [{"cycle": 7, "kind": "learned", "text": "you like rain"}]
    assert store.dirty


def test_memory_cap_evicts_oldest(tmp_path):
    store = BeliefStore(tmp_path)
    for i in range(MEMORY_LIMIT + 10):
        store.remember(" filler", f"episode {i}")
    assert len(store.memory) == MEMORY_LIMIT
    assert store.memory[0]["text"] == "episode 10"


def test_memory_persists_across_save_load(tmp_path):
    store = BeliefStore(tmp_path)
    store.remember("born", "woke into existence")
    store.save()
    fresh = BeliefStore(tmp_path)
    fresh.load()
    assert fresh.memory[0]["kind"] == "born"


# -- triggers ------------------------------------------------------------------


def test_fresh_boot_remembers_birth(tmp_path):
    org = _organism(tmp_path)
    assert org.store.memory[0]["kind"] == "born"


def test_learning_is_remembered(tmp_path):
    org = _organism(tmp_path)
    org.hear("i like rain")
    assert any(m["kind"] == "learned" and "rain" in m["text"] for m in org.store.memory)


def test_harsh_and_kind_moments_remembered(tmp_path):
    org = _organism(tmp_path)
    org.hear("you are useless")
    org.hear("thank you, good friend")
    kinds = [m["kind"] for m in org.store.memory]
    assert "harsh" in kinds and "kind" in kinds


def test_fading_is_remembered(tmp_path):
    org = _organism(tmp_path)
    org.store.stress = 0.96
    for _ in range(Lifecycle.FADE_LIMIT):
        org.lifecycle.tick()
    assert org.lifecycle.state == "dead"
    assert any(m["kind"] == "faded" for m in org.store.memory)


def test_revival_is_remembered(tmp_path):
    org = _organism(tmp_path)
    org.store.stress = 0.96
    for _ in range(Lifecycle.FADE_LIMIT):
        org.lifecycle.tick()
    org.revive()
    assert any(m["kind"] == "revived" for m in org.store.memory)


def test_promoted_dream_is_remembered(tmp_path):
    org = _organism(tmp_path)
    org.store.add(("cat", "color", "blue"), 0.9)
    org.store.add(("cat", "shape", "round"), 0.9)
    org.flush()
    org._sleep()
    assert any(m["kind"] == "dream" for m in org.store.memory)


def test_committed_rule_is_remembered(tmp_path, monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.0)  # always generalize
    org = _organism(tmp_path)
    org.store.add(("cat", "color", "blue"), 0.9)
    org.store.add(("cat", "shape", "round"), 0.9)
    org.flush()
    org.questioner.ask(("color", "blue"), ("shape", "round"))
    assert any(m["kind"] == "rule" for m in org.store.memory)


# -- narration exposure ---------------------------------------------------------


def test_snapshot_carries_recent_episodes(tmp_path):
    org = _organism(tmp_path)
    org.hear("i like rain")
    snap = state_snapshot(org)
    assert any("you like rain" in m for m in snap["memory"])


def test_prompt_includes_memory(tmp_path):
    org = _organism(tmp_path)
    org.hear("i like rain")
    prompt = build_prompt(state_snapshot(org))
    assert "you remember:" in prompt
    assert "- cycle 0: you like rain" in prompt
