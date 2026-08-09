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


# -- tier A: reflection loop + retrieval --------------------------------------

import narration
from organism import Organism
from probe import SystemProbe


def _organism(tmp_path, **kwargs):
    kwargs.setdefault(
        "probe", SystemProbe(proc="/nonexistent/proc", sys="/nonexistent/sys"))
    org = Organism(tmp_path, **kwargs)
    org.load()
    return org


def test_reflect_creates_skill(tmp_path, monkeypatch):
    org = _organism(tmp_path)
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda *a, **k: ("skill: rain talk\n"
                         "when: the user mentions rain\n"
                         "how: connect it to something I know, ask once"))
    result = narration.reflect(org)
    assert result["action"] == "created"
    skill = org.skills.get("rain talk")
    assert skill is not None
    assert "connect" in skill.how


def test_reflect_patches_existing_skill(tmp_path, monkeypatch):
    org = _organism(tmp_path)
    org.skills.save(skills.Skill(name="comfort", when="anxious", how="breathe",
                                 created_cycle=1, updated_cycle=1))
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda *a, **k: "patch: comfort\nwhen: anxious\nhow: breathe slowly")
    result = narration.reflect(org)
    assert result["action"] == "patched"
    assert org.skills.get("comfort").how == "breathe slowly"


def test_reflect_nothing_writes_no_file(tmp_path, monkeypatch):
    org = _organism(tmp_path)
    monkeypatch.setattr("narration._ollama_generate",
                        lambda *a, **k: "nothing")
    assert narration.reflect(org)["action"] == "none"
    assert org.skills.list() == []


def test_reflect_garbage_writes_no_file(tmp_path, monkeypatch):
    org = _organism(tmp_path)
    monkeypatch.setattr("narration._ollama_generate",
                        lambda *a, **k: "I feel like reflecting today!")
    assert narration.reflect(org)["action"] == "none"
    assert org.skills.list() == []


def test_reflect_offline_skips(tmp_path):
    org = _organism(tmp_path)
    narration._voice.online = False
    assert narration.reflect(org)["action"] == "none"
    assert org.skills.list() == []


def test_reflect_prompt_carries_episodes_and_skills(tmp_path, monkeypatch):
    org = _organism(tmp_path)
    org.store.remember("learned", "your name is sam")
    org.skills.save(skills.Skill(name="comfort", when="anxious", how="breathe",
                                 created_cycle=1, updated_cycle=1))
    captured = {}
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda prompt, *a, **k: captured.setdefault("p", prompt) or "nothing")
    narration.reflect(org)
    assert "your name is sam" in captured["p"]
    assert "comfort" in captured["p"]
    assert "patch:" in captured["p"]


def test_relevant_skills_injected_into_prompt(tmp_path):
    org = _organism(tmp_path)
    org.store.add(("user", "like_rain", "true"), 0.8)
    org.skills.save(skills.Skill(
        name="rain talk", when="the user likes rain",
        how="ask one follow-up", created_cycle=0, updated_cycle=0))
    snap = narration.state_snapshot(org)
    assert any("rain talk" in s for s in snap["skills"])
    assert org.skills.get("rain talk").uses == 1
    prompt = narration.build_prompt(snap)
    assert "what you have learned how to do" in prompt


def test_irrelevant_skills_not_injected(tmp_path):
    org = _organism(tmp_path)
    org.skills.save(skills.Skill(
        name="zzz unrelated", when="quantum frobnicate",
        how="nothing", created_cycle=0, updated_cycle=0))
    snap = narration.state_snapshot(org)
    assert snap["skills"] == []


# -- tier A: engine trigger, curation, views ----------------------------------


def test_tick_emits_want_reflect_on_cadence(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.store.cycle = Organism.REFLECT_INTERVAL
    kinds = [e["kind"] for e in org.tick(1.0)]
    assert "want_reflect" in kinds
    kinds = [e["kind"] for e in org.tick(1.0)]
    assert "want_reflect" not in kinds


def test_goal_completion_triggers_reflection(tmp_path):
    org = _organism(tmp_path, wake_seconds=999, sleep_seconds=999)
    org.add_goal("learn about the user")
    org.store.cycle = 5
    org.store.add(("user", "name", "sam"), 0.8)
    org.store.add(("user", "like_rain", "true"), 0.8)
    kinds = [e["kind"] for e in org.tick(1.0)]
    assert "goal" in kinds
    assert "want_reflect" in kinds


def test_flush_curates_stale_skills(tmp_path):
    org = _organism(tmp_path)
    org.skills.save(skills.Skill(name="ancient", when="x", how="y",
                                 created_cycle=0, updated_cycle=0))
    org.store.cycle = 200
    org.flush(force=True)
    assert org.skills.get("ancient") is None
    assert any(m["kind"] == "skill" and "archived" in m["text"]
               for m in org.store.memory)


def test_mind_view_shows_skills(tmp_path):
    import tui_views
    org = _organism(tmp_path)
    org.skills.save(skills.Skill(name="rain talk", when="user likes rain",
                                 how="ask once", created_cycle=0,
                                 updated_cycle=0))
    view = tui_views.mind_view(org)
    assert "skills" in view
    assert "rain talk" in view


# -- tier B: patch proposals ----------------------------------------------------

import extensions


def test_parse_reflect_extension_proposal():
    text = ("patch-extension:\n"
            "kind: pattern\n"
            "entry: i adore (.+) -> user:like_{x}:true\n"
            "example: i adore hiking\n"
            "why: the user says adore and I cannot learn it")
    result = narration.parse_reflect(text)
    assert result["action"] == "proposal"
    entry = result["entry"]
    assert entry["kind"] == "pattern"
    assert entry["regex"] == "i adore (.+)"
    assert entry["template"] == "user:like_{x}:true"
    assert entry["example"] == "i adore hiking"


def test_reflect_proposal_validates_and_pends(tmp_path, monkeypatch):
    org = _organism(tmp_path)
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda *a, **k: ("patch-extension:\n"
                         "kind: pattern\n"
                         "entry: i adore (.+) -> user:like_{x}:true\n"
                         "example: i adore hiking\n"
                         "why: the user says adore"))
    result = narration.reflect(org)
    assert result["action"] == "proposal"
    assert extensions.pending()["regex"] == "i adore (.+)"


def test_reflect_invalid_proposal_becomes_none(tmp_path, monkeypatch):
    org = _organism(tmp_path)
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda *a, **k: ("patch-extension:\n"
                         "kind: pattern\n"
                         "entry: the weather (.+) -> user:like_{x}:true\n"
                         "example: the weather is nice today\n"
                         "why: overreach"))
    assert narration.reflect(org)["action"] == "none"
    assert extensions.pending() is None


def test_reflect_prompt_offers_extension_format(tmp_path, monkeypatch):
    org = _organism(tmp_path)
    captured = {}
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda prompt, *a, **k: captured.setdefault("p", prompt) or "nothing")
    narration.reflect(org)
    assert "patch-extension:" in captured["p"]


def test_parse_reflect_cleans_noisy_name():
    result = narration.parse_reflect(
        "skill: ask    - a new technique worth keeping\n"
        "when: the user is quiet\n"
        "how: let one question hang in the air")
    assert result["name"] == "ask"


def test_parse_reflect_caps_long_names():
    result = narration.parse_reflect(
        "skill: " + "word " * 12 + "\nwhen: x\nhow: y")
    assert len(result["name"].split()) <= 6
