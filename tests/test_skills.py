"""Skills feature (tier A): the organism's procedural memory — plain-text
techniques distilled from experience, stored as markdown files, retrieved
by keyword overlap, curated when stale."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import skills


def _store(tmp_path):
    return skills.SkillStore(tmp_path / "skills")


def test_save_and_get_round_trip(tmp_path):
    store = _store(tmp_path)
    store.save(skills.Skill(
        name="learn from the user",
        when="the user shares personal facts",
        how="acknowledge the fact, connect it, ask one follow-up",
        created_cycle=3, updated_cycle=3))
    skill = store.get("learn from the user")
    assert skill.when == "the user shares personal facts"
    assert "follow-up" in skill.how
    assert skill.uses == 0


def test_patch_preserves_uses_and_bumps_updated(tmp_path):
    store = _store(tmp_path)
    store.save(skills.Skill(name="comfort", when="anxious",
                            how="breathe", created_cycle=1, updated_cycle=1))
    store.record_use("comfort")
    store.record_use("comfort")
    store.save(skills.Skill(name="comfort", when="anxious",
                            how="breathe slowly", created_cycle=1,
                            updated_cycle=5))
    skill = store.get("comfort")
    assert skill.how == "breathe slowly"
    assert skill.uses == 2
    assert skill.updated_cycle == 5


def test_list_scans_files(tmp_path):
    store = _store(tmp_path)
    store.save(skills.Skill(name="a", when="x", how="y",
                            created_cycle=0, updated_cycle=0))
    store.save(skills.Skill(name="b", when="z", how="w",
                            created_cycle=0, updated_cycle=0))
    assert {s.name for s in store.list()} == {"a", "b"}
    # a fresh store over the same dir sees the same skills
    assert {s.name for s in _store(tmp_path).list()} == {"a", "b"}


def test_relevant_matches_keywords(tmp_path):
    store = _store(tmp_path)
    store.save(skills.Skill(name="rain talk", when="the user mentions rain",
                            how="ask about it", created_cycle=0,
                            updated_cycle=0))
    store.save(skills.Skill(name="stress care", when="stress is high",
                            how="slow down", created_cycle=0,
                            updated_cycle=0))
    hits = store.relevant("the user mentioned rain again")
    assert [s.name for s in hits] == ["rain talk"]


def test_archive_stale_moves_unused(tmp_path):
    store = _store(tmp_path)
    store.save(skills.Skill(name="old", when="x", how="y",
                            created_cycle=0, updated_cycle=0))
    store.save(skills.Skill(name="used", when="z", how="w",
                            created_cycle=0, updated_cycle=0))
    store.record_use("used")
    used = store.get("used")
    used.updated_cycle = 90
    store.save(used)
    archived = store.archive_stale(cycle=101, limit=100)
    assert archived == ["old"]
    assert store.get("old") is None
    assert store.get("used") is not None
    assert (tmp_path / "skills" / "archive" / "old.md").exists()
