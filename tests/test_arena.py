"""Thought-arena feature: every utterance the organism manifests runs an
inner debate — two proposers draft a candidate, an adversarial critic
attacks, two voters pick a majority winner (or a random draw when
deadlocked) — with per-round temperature jitter, a chaos-gated rogue
thought (suppressed for structured tasks), and the same local fallbacks
whenever ollama fails."""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from arena import ROGUE_THOUGHT, TEMP_MAX, TEMP_MIN, ThoughtArena
from narration import narrate, respond
from organism import BeliefStore, Lifecycle, Metrics


class _Window:
    def __init__(self):
        self.pairs = {("has_fur", "true")}


class _Org:
    """Minimal organism stand-in (mirrors test_narration.FakeOrg)."""

    def __init__(self, tmp_path):
        self.store = BeliefStore(tmp_path)
        self.store.cycle = 3
        self.store.chaos = 0.5
        self.store.add(("cat", "has_fur", "true"), 0.9)
        self.store.add(("cat", "has_paws", "true"), 0.8)
        self.store.rules.append(
            ('q1(x) = bel(x, "has_fur", "true"), bel(x, "has_paws", "true")',
             1))
        self.lifecycle = Lifecycle(self.store)
        self.window = _Window()

    def metrics(self):
        return Metrics(self.store)

    def chaos_effective(self):
        if self.store.stress > 0.5:
            return min(1.0, self.store.chaos + (self.store.stress - 0.5) * 0.3)
        return self.store.chaos


@pytest.fixture
def org(tmp_path):
    return _Org(tmp_path)


def _scripted(monkeypatch, script):
    """Monkeypatch _ollama_generate with a fixed response script; records
    (prompt, temperature) per call."""
    calls = []

    def fake(prompt, model, timeout, temperature=0.95):
        calls.append((prompt, temperature))
        return script[len(calls) - 1]

    monkeypatch.setattr("narration._ollama_generate", fake)
    return calls


# -- resolution ----------------------------------------------------------

def test_majority_vote_wins(org, monkeypatch):
    calls = _scripted(monkeypatch, [
        "fur and paws",                   # draft 1
        "fur and quiet",                  # draft 2
        "thought 2 is the weaker",        # critique (no VOTE line)
        "VOTE: 2",                        # voter 1
        "VOTE: 2",                        # voter 2
    ])
    assert ThoughtArena().emerge(org) == "fur and quiet"
    assert len(calls) == 5


def test_split_vote_tips_to_critique_preference(org, monkeypatch):
    _scripted(monkeypatch, [
        "fur and paws",
        "fur and quiet",
        "on reflection, VOTE: 1",         # critique prefers draft 1
        "VOTE: 1",
        "VOTE: 2",                        # split vote -> deadlock
    ])
    assert ThoughtArena().emerge(org) == "fur and paws"


def test_indifferent_deadlock_draws_randomly(org, monkeypatch):
    _scripted(monkeypatch, [
        "fur and paws",
        "fur and quiet",
        "they are both merely adequate",  # no preference
        "VOTE: 1",
        "VOTE: 2",
    ])
    result = ThoughtArena(rng=random.Random(3)).emerge(org)
    assert result in ("fur and paws", "fur and quiet")


def test_narrate_runs_a_debate(org, monkeypatch):
    """narrate() delegates to the arena instead of one solo call."""
    calls = _scripted(monkeypatch, [
        "fur and paws",
        "fur and quiet",
        "neither is strong",
        "VOTE: 1",
        "VOTE: 1",
    ])
    assert narrate(org) == "fur and paws"
    assert len(calls) == 5


def test_respond_runs_a_debate(org, monkeypatch):
    """Replies to the user run the same arena: nothing manifests before
    the debate picks a winner."""
    calls = _scripted(monkeypatch, [
        "hello, little one",
        "hi, I suppose",
        "the second is cold",
        "VOTE: 1",
        "VOTE: 1",
    ])
    assert respond(org, "hello there") == "hello, little one"
    assert len(calls) == 5


def test_structured_tasks_never_go_rogue(org, monkeypatch):
    """A rogue candidate would break the output contract of structured
    tasks (reflection format, goals, diary), so it is never injected."""
    calls = []
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda prompt, *a, **k: calls.append(prompt) or "x")
    org.store.chaos = 1.0
    out = ThoughtArena(rng=_AlwaysZero()).emerge(
        org, prompt_kwargs={"reflect": True}, structured=True,
        fallback=lambda _snap: None)
    assert out == "x"
    assert len(calls) == 5
    assert not any(ROGUE_THOUGHT in p for p in calls)
    assert any("Answer in EXACTLY" in p for p in calls)  # reflect task


def test_task_fallback_used_on_debate_failure(org, monkeypatch):
    def boom(prompt, model, timeout, temperature=0.95):
        raise RuntimeError("ollama down")

    monkeypatch.setattr("narration._ollama_generate", boom)
    out = ThoughtArena().emerge(org, prompt_kwargs={"ask_user": True},
                                fallback=lambda _snap: "fallback question?")
    assert out == "fallback question?"


# -- nonlinearity ----------------------------------------------------------

def test_temperature_jitters_per_round(org, monkeypatch):
    calls = _scripted(monkeypatch, ["fur and paws"] * 5)
    ThoughtArena(rng=random.Random(11)).emerge(org)
    temps = [t for _p, t in calls]
    assert len(set(temps)) > 1
    assert all(TEMP_MIN <= t <= TEMP_MAX for t in temps)


class _AlwaysZero:
    """RNG that always fires the surprise check (random() == 0.0)."""

    def random(self):
        return 0.0

    def choice(self, seq):
        return seq[0]


class _Half:
    """RNG that never fires the surprise check (random() == 0.5)."""

    def random(self):
        return 0.5

    def choice(self, seq):
        return seq[0]


class _Fixed:
    """RNG returning a fixed random() value; probes a specific surprise
    threshold."""

    def __init__(self, val):
        self.val = val

    def random(self):
        return self.val

    def choice(self, seq):
        return seq[0]


def test_rogue_thought_fires_in_high_chaos(org, monkeypatch):
    calls = []

    def fake(prompt, model, timeout, temperature=0.95):
        calls.append(prompt)
        if ROGUE_THOUGHT in prompt:
            return "i am a rogue thought, watch me"
        return "ordinary thought"

    monkeypatch.setattr("narration._ollama_generate", fake)
    org.store.chaos = 0.7
    ThoughtArena(rng=_AlwaysZero()).emerge(org)
    assert len(calls) == 5
    assert any(ROGUE_THOUGHT in p for p in calls)
    assert any("watch me" in p for p in calls[2:])


def test_no_rogue_thought_in_low_chaos(org, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda prompt, model, timeout, temperature=0.95:
            calls.append(prompt) or "ordinary thought")
    org.store.chaos = 0.0
    ThoughtArena(rng=_Half()).emerge(org)
    assert not any(ROGUE_THOUGHT in p for p in calls)


def test_stress_nudges_surprise_via_effective_chaos(org, monkeypatch):
    """Raw chaos 0.2 is below every surprise level (default 0.02), but
    stress 0.95 nudges effective chaos to ~0.34, crossing the 0.3 level
    (0.05) - so a random() of 0.03 fires the rogue thought."""
    calls = []
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda prompt, model, timeout, temperature=0.95:
            calls.append(prompt) or "ordinary thought")
    org.store.chaos = 0.2
    org.store.stress = 0.95
    ThoughtArena(rng=_Fixed(0.03)).emerge(org)
    assert any(ROGUE_THOUGHT in p for p in calls)


# -- fallbacks -------------------------------------------------------------

def test_late_round_failure_falls_back(org, monkeypatch):
    """A failure during the critique round still falls back: the debate
    must not swallow an error into a broken half-debate."""

    def critic_dies(prompt, model, timeout, temperature=0.95):
        if "Attack both candidates" in prompt:
            raise RuntimeError("critic died")
        return "fur and paws"

    monkeypatch.setattr("narration._ollama_generate", critic_dies)
    text = ThoughtArena().emerge(org)
    assert "2 beliefs" in text and "wake" in text


def test_respond_falls_back_on_arena_failure(org, monkeypatch):
    def boom(prompt, model, timeout, temperature=0.95):
        raise RuntimeError("ollama down")

    monkeypatch.setattr("narration._ollama_generate", boom)
    reply = respond(org, "hello there")
    assert "hello there" in reply and "2 beliefs" in reply


# -- candidate cleaning -----------------------------------------------------

def test_meta_preamble_unwrapped_from_draft(org, monkeypatch):
    """Chatty models preface the candidate with "Here is a draft of a
    candidate answer:" — only the answer itself may manifest."""
    _scripted(monkeypatch, [
        "Here is a draft of a candidate answer:\n\nfur and paws",
        "Here is the draft:\nfur and quiet",
        "both weak",
        "VOTE: 1",
        "VOTE: 1",
    ])
    assert ThoughtArena().emerge(org) == "fur and paws"


def test_trailing_self_evaluation_cut(org, monkeypatch):
    """A draft that rambles on into its own evaluation is cut at the
    evaluation boundary."""
    _scripted(monkeypatch, [
        "fur and paws\n\nHere is the evaluation: it is quite good",
        "fur and quiet",
        "both weak",
        "VOTE: 1",
        "VOTE: 1",
    ])
    assert ThoughtArena().emerge(org) == "fur and paws"


def test_special_token_loop_cut(org, monkeypatch):
    """Models that miss their stop token loop <|im_start|> forever; the
    garbage must never reach an utterance."""
    _scripted(monkeypatch, [
        "fur and paws",
        "fur and quiet",
        "both weak",
        "VOTE: 1",
        "VOTE: 1",
    ])
    # _strip_special lives in narration._ollama_generate; simulate its
    # effect here by checking the cleaner directly
    from narration import _strip_special
    assert _strip_special(
        "hello there<|endoftext|><|im_start|>\n<|im_start|>") == "hello there"


def test_single_usable_draft_wins_outright(org, monkeypatch):
    """When one proposer returns only meta-narration, the surviving draft
    wins without paying for the critique and vote rounds."""
    calls = _scripted(monkeypatch, [
        "Here is a draft of a candidate answer:\n\n",   # empty after clean
        "fur and quiet",
    ])
    assert ThoughtArena().emerge(org) == "fur and quiet"
    assert len(calls) == 2


def test_no_usable_draft_falls_back(org, monkeypatch):
    """If both proposers return nothing usable, the debate fails and the
    local deterministic fallback answers instead."""
    _scripted(monkeypatch, ["", "Here is a draft of a candidate answer:"])
    text = ThoughtArena().emerge(org)
    assert "2 beliefs" in text and "wake" in text


def test_repetition_loop_counts_as_no_candidate(org, monkeypatch):
    """A proposer stuck looping the same sentence has no candidate; the
    surviving draft wins outright."""
    loop = "\n".join(["Do not wrap the text in any Markdown."] * 9)
    calls = _scripted(monkeypatch, [loop, "fur and quiet"])
    assert ThoughtArena().emerge(org) == "fur and quiet"
    assert len(calls) == 2


def test_all_loops_fall_back(org, monkeypatch):
    """When every proposer is stuck in a repetition loop the debate fails
    and the local fallback answers."""
    loop = "\n".join(["I am thinking about thinking."] * 5)
    _scripted(monkeypatch, [loop, loop])
    text = ThoughtArena().emerge(org)
    assert "2 beliefs" in text and "wake" in text


def test_anaphora_loop_counts_as_no_candidate(org, monkeypatch):
    """Sentences that keep opening with the same words ('The first was …;
    The first was …') are a stuck generator even though no two sentences
    are identical."""
    loop = ("The first was soft and static; this one should be sharp. "
            "The first was about finding me; this one should be seeing. "
            "The first was about holding beliefs; this one is losing. "
            "The first was about the name; this one is about the echo.")
    calls = _scripted(monkeypatch, [loop, "fur and quiet"])
    assert ThoughtArena().emerge(org) == "fur and quiet"
    assert len(calls) == 2


def test_clean_candidate_strips_prompt_scaffolding():
    """Echoed build_prompt instruction lines are scaffolding, not speech."""
    from arena import _clean_candidate
    raw = ("I keep my beliefs close.\n"
           "Answer your own question, as the organism itself. First\n"
           "person, one to three sentences. No preamble, no quotes,\n"
           "no emoji.\n"
           "Speak from feeling; never recite statistics.\n"
           "Never use these worn-out words: astonished, tender, wonder.")
    assert _clean_candidate(raw) == "I keep my beliefs close."


def test_clean_candidate_strips_group_context_scaffolding():
    from arena import _clean_candidate
    raw = ("The user just said: You are in a group chat with John, "
           "stephanie and the user.\n"
           "Reply to the group as yourself, in one to three sentences.\n"
           "Rain makes the attic feel smaller, in a good way.")
    assert _clean_candidate(raw) == \
        "Rain makes the attic feel smaller, in a good way."


def test_clean_candidate_strips_rogue_thought_echo():
    """A partial echo of the ROGUE_THOUGHT instruction (just the first
    sentence, without the marker tail) is scaffolding, not speech."""
    from arena import ROGUE_THOUGHT, _clean_candidate
    first_sentence = ROGUE_THOUGHT.split(". ")[0] + "."
    assert "no preamble" not in first_sentence.lower()  # the uncovered case
    raw = first_sentence + "\nThe cellar smells of rain tonight."
    assert _clean_candidate(raw) == "The cellar smells of rain tonight."
    # verbatim full echo (with the marker tail) is also stripped
    assert _clean_candidate(ROGUE_THOUGHT) == ""


def test_clean_candidate_keeps_genuine_first_person():
    from arena import _clean_candidate
    raw = "I asked myself about the rain, and I still have no answer."
    assert _clean_candidate(raw) == raw
