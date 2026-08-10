"""Learning feature: pattern-based fact extraction from user chat,
vocabulary sanitization, hear() assimilation into the belief store,
and narration exposure of user facts."""

from replicanta import learning
from replicanta.learning import describe, extract
from replicanta.narration import build_prompt, state_snapshot
from replicanta.organism import Organism
from replicanta.probe import SystemProbe


def _organism(tmp_path):
    org = Organism(
        tmp_path, probe=SystemProbe(proc="/nonexistent/proc", sys="/nonexistent/sys")
    )
    org.load()
    return org


def _beliefs_only(facts):
    return [b for b, _replace in facts]


# -- extraction ---------------------------------------------------------------


def test_extract_name():
    assert _beliefs_only(extract("my name is Sam")) == [("user", "name", "sam")]


def test_extract_like_multiword():
    assert _beliefs_only(extract("i really like ice cream")) == [
        ("user", "like_ice_cream", "true")
    ]


def test_extract_dislike():
    assert _beliefs_only(extract("i hate loud noises")) == [
        ("user", "dislike_loud_noises", "true")
    ]


def test_extract_feeling():
    assert _beliefs_only(extract("i am happy")) == [("user", "feeling", "happy")]
    assert _beliefs_only(extract("i feel sad.")) == [("user", "feeling", "sad")]


def test_extract_you_are():
    assert _beliefs_only(extract("you are beautiful")) == [
        ("self", "described_as", "beautiful")
    ]


def test_extract_your_trait():
    assert _beliefs_only(extract("your color is blue")) == [("self", "color", "blue")]


def test_extract_strips_filler():
    assert _beliefs_only(extract("i like rain a lot")) == [
        ("user", "like_rain", "true")
    ]


def test_questions_teach_nothing():
    assert extract("do you like rain?") == []
    assert extract("what is my name?") == []


def test_unlearnable_text_yields_nothing():
    assert extract("asdf 1234 !!!") == []
    assert extract("hello there little one") == []


def test_extract_caps_per_message():
    facts = extract("my name is Sam and i like rain and you are brave")
    assert len(facts) <= learning.MAX_PER_MESSAGE


# -- describe -----------------------------------------------------------------


def test_describe_user_facts():
    assert describe(("user", "name", "sam")) == "your name is sam"
    assert describe(("user", "like_ice_cream", "true")) == "you like ice cream"
    assert describe(("user", "feeling", "happy")) == "you feel happy"
    assert describe(("self", "described_as", "brave")) == "you say I am brave"
    assert describe(("self", "color", "blue")) == "my color is blue"


# -- hear() assimilation --------------------------------------------------------


def test_hear_learns_and_reports(tmp_path):
    org = _organism(tmp_path)
    events = org.hear("my name is Sam")
    assert org.store.conf(("user", "name", "sam")) == 0.8
    learned = [e for e in events if e["kind"] == "learned"]
    assert learned[0]["text"] == "your name is sam"


def test_hear_learned_facts_persist(tmp_path):
    org = _organism(tmp_path)
    org.hear("i like rain")
    org.flush()
    fresh = Organism(
        tmp_path, probe=SystemProbe(proc="/nonexistent/proc", sys="/nonexistent/sys")
    )
    fresh.load()
    assert fresh.store.conf(("user", "like_rain", "true")) == 0.8


def test_multiple_likes_accumulate(tmp_path):
    org = _organism(tmp_path)
    org.hear("i like rain")
    org.hear("i like snow")
    assert org.store.conf(("user", "like_rain", "true")) == 0.8
    assert org.store.conf(("user", "like_snow", "true")) == 0.8


def test_feeling_supersedes(tmp_path):
    org = _organism(tmp_path)
    org.hear("i am happy")
    org.hear("i am tired")
    assert org.store.conf(("user", "feeling", "tired")) == 0.8
    assert ("user", "feeling", "happy") not in org.store.beliefs()


def test_learning_makes_it_curious(tmp_path):
    org = _organism(tmp_path)
    events = org.hear("i like rain")
    assert {"kind": "mood", "mood": "curious"} in events


def test_learned_facts_render_into_genome(tmp_path):
    org = _organism(tmp_path)
    org.hear("i like rain")
    org.flush()
    assert (
        'rel 0.8::bel("user", "like_rain", "true")'
        in (tmp_path / "organism.scl").read_text()
    )


# -- narration exposure ---------------------------------------------------------


def test_snapshot_lists_user_facts(tmp_path):
    org = _organism(tmp_path)
    org.hear("my name is Sam")
    org.hear("i like rain")
    snap = state_snapshot(org)
    assert "your name is sam" in snap["user_facts"]
    assert "you like rain" in snap["user_facts"]


def test_snapshot_captures_user_view(tmp_path):
    org = _organism(tmp_path)
    org.hear("you are brave")
    assert state_snapshot(org)["user_view"] == "brave"


def test_prompt_includes_user_facts(tmp_path):
    org = _organism(tmp_path)
    org.hear("my name is Sam")
    prompt = build_prompt(state_snapshot(org))
    assert "what you know about the user:" in prompt
    assert "- your name is sam" in prompt
