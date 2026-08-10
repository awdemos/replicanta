"""Activity-meter feature: exact neurosymbolic event counters — symbolic
(derivations, beliefs, rules, dreams), neural (llm calls, tokens,
utterances, fallbacks), coupling (facts learned, lexical grounding) —
persisted with state.json and surfaced as totals + per-cycle rates."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import activity
import llmclient
from arena import ThoughtArena
from organism import BeliefStore, Organism
from probe import SystemProbe
from conftest import patch_generate

FEELING = ("cat", "has_fur", "true")
PAWS = ("cat", "has_paws", "true")


def _org(tmp_path):
    org = Organism(tmp_path, probe=SystemProbe(proc="/nonexistent/proc",
                                               sys="/nonexistent/sys"))
    org.load()
    return org


# -- belief-store counters ----------------------------------------------------

def test_add_counts_new_and_strengthened(tmp_path):
    store = BeliefStore(tmp_path)
    store.add(FEELING, 0.5)
    assert store.activity["beliefs_new"] == 1
    store.add(FEELING, 0.4)          # weaker: no change, no count
    assert "beliefs_strengthened" not in store.activity
    store.add(FEELING, 0.9)
    assert store.activity["beliefs_strengthened"] == 1


def test_contradiction_counts_archived(tmp_path):
    store = BeliefStore(tmp_path)
    store.add(("cat", "mood", "calm"), 0.9)
    store.add(("cat", "mood", "angry"), 0.95)   # wins, archives calm
    assert store.activity["beliefs_archived"] == 1


def test_commit_rule_counts(tmp_path):
    store = BeliefStore(tmp_path)
    store.commit_rule('q1(x) = bel(x, "has_fur", "true")', 1)
    assert store.activity["rules_committed"] == 1


def test_activity_persists_with_state(tmp_path):
    store = BeliefStore(tmp_path)
    store.add(FEELING, 0.9)
    store.commit_rule('q1(x) = bel(x, "has_fur", "true")', 1)
    store.save()
    fresh = BeliefStore(tmp_path)
    fresh.load()
    assert fresh.activity["beliefs_new"] == 1
    assert fresh.activity["rules_committed"] == 1


# -- symbolic call sites --------------------------------------------------------

def test_self_question_counts_rules_tried_and_derivations(tmp_path):
    org = _org(tmp_path)
    org.store.add(FEELING, 0.9)
    org.store.add(PAWS, 0.9)
    org.flush(force=True)   # render genome + rebuild the reasoner
    before = dict(org.store.activity)
    org.questioner.ask(("has_fur", "true"), ("has_paws", "true"))
    assert org.store.activity["rules_tried"] == before.get("rules_tried", 0) + 1
    assert org.store.activity.get("derivations", 0) >= 1


def test_dream_validation_counts_promoted_and_discarded(tmp_path):
    org = _org(tmp_path)
    org.store.add(FEELING, 0.9)
    org.store.add(PAWS, 0.9)
    org.flush(force=True)
    dreams = org.dreamer.dream(count=1)
    org.store.chaos = 0.0   # no random rule-commit muddies the counts
    org.dreamer.promote(dreams)
    a = org.store.activity
    assert a.get("dreams_promoted", 0) + a.get("dreams_discarded", 0) == 1


def test_hear_counts_facts_learned(tmp_path):
    org = _org(tmp_path)
    events = org.hear("my name is sam")
    assert any(e["kind"] == "learned" for e in events)
    assert org.store.activity["facts_learned"] == 1


# -- neural + coupling (via the arena) -----------------------------------------

def _scripted_arena(monkeypatch, script, gen_tokens=3, prompt_tokens=11):
    def fake(prompt, model, timeout, temperature=0.95):
        return (script.pop(0), {"prompt_tokens": prompt_tokens,
                                "gen_tokens": gen_tokens})

    monkeypatch.setattr("llmclient.generate_with_stats", fake)


def test_arena_meters_calls_tokens_and_utterance(tmp_path, monkeypatch):
    org = _org(tmp_path)
    _scripted_arena(monkeypatch, [
        "the cat again", "dogs, maybe", "both weak", "VOTE: 1", "VOTE: 1"])
    text = ThoughtArena().emerge(org)
    assert text == "the cat again"
    a = org.store.activity
    assert a["llm_calls"] == 5
    assert a["gen_tokens"] == 15
    assert a["prompt_tokens"] == 55
    assert a["utterances"] == 1


def test_arena_counts_fallbacks(tmp_path, monkeypatch):
    org = _org(tmp_path)

    def boom(prompt, model, timeout, temperature=0.95):
        raise RuntimeError("ollama down")

    patch_generate(monkeypatch, boom)
    ThoughtArena().emerge(org)
    assert org.store.activity["fallbacks"] == 1


def test_grounded_utterance_counted_when_seed_words_reused(tmp_path, monkeypatch):
    org = _org(tmp_path)
    org.store.add(FEELING, 0.9)   # seeds will mention has_fur / cat
    seen = {}

    def fake(prompt, model, timeout, temperature=0.95):
        if "Candidate" in prompt:
            return seen.setdefault("winner", "thinking about the cat today")
        return seen.setdefault("winner", "thinking about the cat today")

    patch_generate(monkeypatch, fake)
    # craft the seed deterministically: the belief itself
    monkeypatch.setattr(llmclient, "seed_for",
                        lambda snap, rng, exclude=():
                        "this belief: 0.90 cat:has_fur=true")
    text = ThoughtArena().emerge(org)
    assert "cat" in text
    assert org.store.activity["grounded_utterances"] == 1


def test_grounding_proxy():
    assert activity.grounded("this belief: 0.90 cat:has_fur=true",
                             "I keep thinking about the cat")
    assert not activity.grounded("this belief: 0.90 cat:has_fur=true",
                                 "the rain outside is lovely")
    # scaffold words alone never count as grounding
    assert not activity.grounded("your calm mood", "the rain outside")


# -- summary --------------------------------------------------------------------

def test_summary_empty_until_activity(tmp_path):
    store = BeliefStore(tmp_path)
    assert activity.summary_lines(store) == []


def test_summary_reports_all_three_groups(tmp_path):
    org = _org(tmp_path)
    org.store.cycle = 4
    org.store.note_activity("derivations", 8)
    org.store.note_activity("llm_calls", 5)
    org.store.note_activity("facts_learned", 2)
    text = "\n".join(activity.summary_lines(org.store))
    assert "symbolic:" in text and "8 derivations" in text
    assert "neural:" in text and "5 llm calls" in text
    assert "coupling:" in text and "2 facts learned" in text
    assert "2.00/cycle" in text        # 8 derivations / 4 cycles
