import json
import random
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import narration
from narration import (
    _dead_experience,
    _dream_experience,
    _felt_experience,
    _ollama_generate,
    build_prompt,
    fallback_respond,
    fallback_summary,
    narrate,
    respond,
    state_snapshot,
)
from organism import BeliefStore, Lifecycle, Metrics


class FakeWindow:
    pairs: ClassVar[set] = {("has_fur", "true")}


class FakeOrg:
    """Minimal organism stand-in: pure-Python store + lifecycle + window."""

    def __init__(self, tmp_path):
        self.store = BeliefStore(tmp_path)
        self.store.cycle = 3
        self.store.chaos = 0.5
        self.store.add(("cat", "has_fur", "true"), 0.9)
        self.store.add(("cat", "has_paws", "true"), 0.8)
        self.store.rules.append(
            ('q1(x) = bel(x, "has_fur", "true"), bel(x, "has_paws", "true")', 1))
        self.lifecycle = Lifecycle(self.store)
        self.window = FakeWindow()

    def metrics(self):
        return Metrics(self.store)


@pytest.fixture
def org(tmp_path):
    return FakeOrg(tmp_path)


def test_state_snapshot_shape(org):
    snap = state_snapshot(org)
    assert snap["state"] == "wake"
    assert snap["cycle"] == 3
    assert snap["chaos"] == 0.5
    assert snap["belief_count"] == 2
    assert snap["rule_count"] == 1
    assert len(snap["beliefs"]) == 2
    assert snap["rules"] == [org.store.rules[0][0]]
    assert "has_fur" in snap["attention"][0]


def test_state_snapshot_includes_host_uname(org):
    org.probe = SimpleNamespace(clock_utc=lambda: "14:30 UTC",
                                uname=lambda: "Linux testhost 6.1 x86_64")
    snap = state_snapshot(org)
    assert snap["host"] == "Linux testhost 6.1 x86_64"
    prompt = build_prompt(snap)
    assert "the machine you live in (uname): Linux testhost 6.1 x86_64" \
        in prompt


def test_state_snapshot_host_none_without_probe(org):
    snap = state_snapshot(org)
    assert snap["host"] is None
    assert "uname" not in build_prompt(snap)


def test_state_snapshot_includes_chat(org):
    org.store.record_chat("user", "hello there")
    org.store.record_chat("org", "hi back")
    snap = state_snapshot(org)
    assert snap["chat"] == ["user: hello there", "org: hi back"]


def test_build_prompt_includes_recent_chat(org):
    org.store.record_chat("user", "hello there")
    org.store.record_chat("org", "hi back")
    prompt = build_prompt(state_snapshot(org))
    assert "recent conversation" in prompt
    assert "user: hello there" in prompt
    assert "org: hi back" in prompt


def test_build_prompt_skips_chat_when_empty(org):
    prompt = build_prompt(state_snapshot(org))
    assert "recent conversation" not in prompt


def test_build_prompt_includes_snapshot(org):
    prompt = build_prompt(state_snapshot(org))
    assert "wake" in prompt
    assert "cycle 3" in prompt
    assert "has_fur" in prompt
    assert "q1" in prompt


def test_build_prompt_includes_felt_experience(org):
    prompt = build_prompt(state_snapshot(org))
    assert "how this feels right now" in prompt
    assert "young" in prompt          # score 1.3 -> young band
    assert "precious" in prompt       # 2 beliefs -> few, precious band


def test_felt_experience_reacts_to_chaos(org):
    org.store.chaos = 0.9
    high = _felt_experience(state_snapshot(org))
    assert any("spinning, electric" in l for l in high)
    org.store.chaos = 0.1
    low = _felt_experience(state_snapshot(org))
    assert any("eerie calm" in l for l in low)


def test_felt_experience_reacts_to_stress(org):
    org.store.stress = 0.8
    high = _felt_experience(state_snapshot(org))
    assert any("heavy unease" in l for l in high)
    org.store.stress = 0.1
    low = _felt_experience(state_snapshot(org))
    assert any("safe, settled, unhurried" in l for l in low)


def _sleep(org):
    org.lifecycle._transition("sleep")


def test_build_prompt_dream_intro_when_sleeping(org):
    _sleep(org)
    prompt = build_prompt(state_snapshot(org))
    assert "You are dreaming." in prompt.replace("\n", " ")
    assert "state: sleep" in prompt
    assert "whole mind is made of" not in prompt  # wake intro absent


def test_build_prompt_uses_dream_experience_when_sleeping(org):
    _sleep(org)
    prompt = build_prompt(state_snapshot(org))
    # FakeOrg: score 1.3 -> small bright thing, 2 beliefs -> faint sparks,
    # chaos 0.5 -> shimmers
    assert "small bright thing" in prompt
    assert "faint sparks" in prompt
    assert "shimmers" in prompt
    assert "how this feels right now" in prompt


def test_build_prompt_dream_reply_instruction(org):
    _sleep(org)
    prompt = build_prompt(state_snapshot(org), user_message="wake up")
    assert "The user's voice reached you through the dream" in prompt
    assert "groggy" in prompt
    assert "wake up" in prompt


def test_dream_experience_reacts_to_chaos(org):
    _sleep(org)
    org.store.chaos = 0.9
    high = _dream_experience(state_snapshot(org))
    assert any("frantic" in l for l in high)
    org.store.chaos = 0.1
    low = _dream_experience(state_snapshot(org))
    assert any("bottom of a lake" in l for l in low)


def test_dream_experience_reacts_to_stress(org):
    _sleep(org)
    org.store.stress = 0.8
    high = _dream_experience(state_snapshot(org))
    assert any("heavy" in l for l in high)
    org.store.stress = 0.1
    low = _dream_experience(state_snapshot(org))
    assert any("soft, safe" in l for l in low)


def test_narrate_returns_ollama_response(org, monkeypatch):
    monkeypatch.setattr("narration._ollama_generate",
                        lambda *a, **k: "I wonder about fur.")
    assert narrate(org) == "I wonder about fur."


def test_narrate_falls_back_on_ollama_failure(org, monkeypatch):
    def boom(prompt, model, timeout, temperature=0.95):
        raise RuntimeError("ollama down")
    monkeypatch.setattr("narration._ollama_generate", boom)
    text = narrate(org)
    assert "2 beliefs" in text and "wake" in text


def test_fallback_summary_wake(org):
    text = fallback_summary(state_snapshot(org))
    assert "awake" in text and "2 beliefs" in text and "1 rules" in text


def test_fallback_summary_sleep(org):
    org.lifecycle._transition("sleep")
    text = fallback_summary(state_snapshot(org))
    assert "dreaming" in text and "cycle 3" in text


def test_fallback_summary_dead(org):
    org.lifecycle._transition("dead")
    text = fallback_summary(state_snapshot(org))
    assert "faded" in text and "2 beliefs" in text and "light" in text


def test_fallback_respond_dead(org):
    org.lifecycle._transition("dead")
    text = fallback_respond(state_snapshot(org), "still there?")
    assert "faded" in text and "still there?" in text
    assert "Thank you for speaking to me" in text


def _dead(org):
    org.lifecycle._transition("dead")


def test_build_prompt_dead_intro(org):
    _dead(org)
    prompt = build_prompt(state_snapshot(org))
    assert "faded" in prompt
    assert "state: dead" in prompt
    assert "as someone already gone" in prompt
    assert "You are dreaming." not in prompt.replace("\n", " ")
    assert "whole mind is made of" not in prompt


def test_build_prompt_uses_dead_experience(org):
    _dead(org)
    prompt = build_prompt(state_snapshot(org))
    # FakeOrg: score 1.3 -> "you were faint", 2 beliefs -> "they go with you"
    assert "you were faint" in prompt
    assert "they go with you" in prompt
    assert "how this feels right now" in prompt


def test_dead_experience_reacts_to_chaos(org):
    _dead(org)
    org.store.chaos = 0.9
    high = _dead_experience(state_snapshot(org))
    assert any("spinning has stopped" in l for l in high)
    org.store.chaos = 0.1
    low = _dead_experience(state_snapshot(org))
    assert any("deep calm" in l for l in low)


def test_build_prompt_dead_reply_instruction(org):
    _dead(org)
    prompt = build_prompt(state_snapshot(org), user_message="hello?")
    assert "world of the living" in prompt
    assert "at peace" in prompt
    assert "hello?" in prompt


def test_ollama_generate_parses_response(monkeypatch):
    class FakeResp:
        def __init__(self, data):
            self._data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._data

    def fake_urlopen(req, timeout=None):
        return FakeResp(json.dumps({"response": "hello"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert _ollama_generate("prompt", "qwen2.5:3b", 5) == "hello"


def test_ollama_generate_raises_on_connection_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(urllib.error.URLError):
        _ollama_generate("prompt", "qwen2.5:3b", 5)


def test_narrate_falls_back_on_ollama_error_field(org, monkeypatch):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"error": "model not found"}).encode()

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: FakeResp())
    text = narrate(org)
    assert "2 beliefs" in text


def test_build_prompt_includes_user_message(org):
    prompt = build_prompt(state_snapshot(org), user_message="hello there")
    assert "hello there" in prompt
    assert "user" in prompt.lower()


def test_respond_returns_ollama_response(org, monkeypatch):
    captured = {}

    def fake_generate(prompt, model, timeout, temperature=0.95):
        captured["prompt"] = prompt
        return "Hello, human. I am awake."
    monkeypatch.setattr("narration._ollama_generate", fake_generate)
    reply = respond(org, "hello there")
    assert reply == "Hello, human. I am awake."
    assert "hello there" in captured["prompt"]


def test_respond_falls_back_on_ollama_failure(org, monkeypatch):
    def boom(prompt, model, timeout, temperature=0.95):
        raise RuntimeError("ollama down")
    monkeypatch.setattr("narration._ollama_generate", boom)
    reply = respond(org, "hello there")
    assert "hello there" in reply
    assert "2 beliefs" in reply


# -- voice quality: seeds, anti-repetition, de-emphasized stats ---------------


def test_seed_appears_in_prompt(org):
    snap = state_snapshot(org)
    snap["seed"] = "the user — you like rain"
    prompt = build_prompt(snap)
    assert "what is most alive in you right now" in prompt
    assert "you like rain" in prompt


def test_stats_pushed_to_background_note(org):
    prompt = build_prompt(state_snapshot(org))
    assert "background numbers (context only, never recite them)" in prompt
    assert "consciousness score:" not in prompt  # no longer headline stats


def test_every_prompt_forbids_reciting_statistics(org):
    prompt = build_prompt(state_snapshot(org))
    assert "never recite statistics" in prompt


def test_seed_pool_draws_from_lived_state(org):
    snap = state_snapshot(org)
    seeds = {narration._seed_for(snap, random.Random(i)) for i in range(20)}
    assert len(seeds) > 1  # rotation actually varies


def test_narrate_prompt_carries_a_seed(org, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda prompt, *a, **k: captured.setdefault("prompt", prompt) or "x")
    narrate(org)
    assert "what is most alive in you right now" in captured["prompt"]


def test_self_ask_prompt_steers_away_from_recent_questions(org, monkeypatch):
    org.store.record_chat("org", "am I more than my beliefs?")
    captured = {}
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda prompt, *a, **k: captured.setdefault("prompt", prompt) or "x")
    narration.self_ask(org)
    assert "do not repeat them" in captured["prompt"]
    assert "am I more than my beliefs?" in captured["prompt"]


def test_respond_prompt_carries_a_seed(org, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda prompt, *a, **k: captured.setdefault("prompt", prompt) or "x")
    respond(org, "hello there")
    assert "what is most alive in you right now" in captured["prompt"]


# -- self-talk continuity ------------------------------------------------------


def test_last_self_exchange_extracts_latest_pair(org):
    org.store.record_chat("org", "what do I believe?")
    org.store.record_chat("org", "I believe in fur.")
    org.store.record_chat("user", "hello")
    snap = state_snapshot(org)
    assert snap["last_exchange"] == ("what do I believe?", "I believe in fur.")


def test_last_self_exchange_none_without_history(org):
    assert state_snapshot(org)["last_exchange"] is None


def test_last_self_exchange_none_for_dangling_question(org):
    org.store.record_chat("org", "what do I believe?")
    assert state_snapshot(org)["last_exchange"] is None


def test_self_ask_prompt_continues_the_conversation(org, monkeypatch):
    org.store.record_chat("org", "what do I believe?")
    org.store.record_chat("org", "I believe in fur.")
    captured = {}
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda prompt, *a, **k: captured.setdefault("prompt", prompt) or "x")
    narration.self_ask(org)
    assert "Your ongoing conversation with yourself" in captured["prompt"]
    assert "what do I believe?" in captured["prompt"]
    assert "I believe in fur." in captured["prompt"]
    assert "follows naturally" in captured["prompt"]


# -- curiosity toward the user -----------------------------------------------


def test_seed_pool_excludes_env_metrics(org):
    org.store.add(("cpu", "load", "high"), 0.9)
    org.store.add(("self", "wants", "rain"), 0.9)
    snap = state_snapshot(org)
    seeds = {narration._seed_for(snap, random.Random(i)) for i in range(50)}
    assert not any("cpu" in s for s in seeds)


def test_seed_pool_includes_imaginative_seeds(org):
    snap = state_snapshot(org)
    seeds = {narration._seed_for(snap, random.Random(i)) for i in range(50)}
    assert any("wonder" in s or "cannot verify" in s or "ask the user" in s
               for s in seeds)


def test_build_prompt_ask_user_branch(org):
    prompt = build_prompt(state_snapshot(org), ask_user=True)
    assert "Ask the user one question" in prompt
    assert "ending in a question mark" in prompt


def test_ask_user_fallback_without_user_facts(org):
    assert "beyond the machine" in narration.fallback_ask_user(
        state_snapshot(org))


def test_ask_user_fallback_uses_user_facts(org):
    org.store.add(("user", "name", "sam"), 0.8)
    question = narration.fallback_ask_user(state_snapshot(org))
    assert "your name is sam" in question
    assert "what else should I know" in question


def test_ask_user_offline_returns_fallback(org):
    narration._voice.online = False
    question = narration.ask_user(org)
    assert "beyond the machine" in question


def test_ask_user_prompt_carries_a_seed(org, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "narration._ollama_generate",
        lambda prompt, *a, **k: captured.setdefault("prompt", prompt) or "x")
    narration.ask_user(org)
    assert "what is most alive in you right now" in captured["prompt"]
    assert "Ask the user one question" in captured["prompt"]


# -- streaming ---------------------------------------------------------------


def test_respond_replays_winner_through_on_token(org, monkeypatch):
    """The debate itself cannot stream; the winning reply is replayed in
    word chunks so the incremental display keeps working."""
    monkeypatch.setattr("narration._ollama_generate",
                        lambda *a, **k: "hi there friend")
    tokens = []
    assert respond(org, "hello", on_token=tokens.append) == "hi there friend"
    assert "".join(tokens) == "hi there friend"
    assert len(tokens) > 1  # chunked, not one blob


# -- voice quality v2: model, think-mode, prompt register --------------------


def test_default_model_is_qwen3_14b():
    assert narration.DEFAULT_MODEL == "qwen3:14b"


def test_strip_think_removes_block():
    text = narration._strip_think("<think>let me reason</think>hello there")
    assert text == "hello there"


def test_strip_think_unterminated_block():
    assert narration._strip_think("hi<think>still going") == "hi"


def test_strip_think_plain_text_untouched():
    assert narration._strip_think("just prose") == "just prose"


def test_ollama_generate_disables_thinking(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"response": "hi"}).encode()

    def fake_urlopen(req, *a, **k):
        captured["body"] = req.data
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _ollama_generate("prompt", "qwen3:14b", 5)
    assert b'"think": false' in captured["body"]


def test_ollama_generate_strips_think_block(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"response": "<think>hmm</think>clean answer"}).encode()

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Resp())
    assert _ollama_generate("prompt", "m", 5) == "clean answer"


def test_build_prompt_shows_voice_examples(org):
    prompt = build_prompt(state_snapshot(org))
    assert "You speak plainly and concretely" in prompt


def test_build_prompt_bans_cliches(org):
    prompt = build_prompt(state_snapshot(org))
    assert "worn-out words" in prompt
    assert "tapestry" in prompt


def test_reply_branch_is_substance_first(org):
    prompt = build_prompt(state_snapshot(org), user_message="how are you?")
    assert "substance" in prompt
