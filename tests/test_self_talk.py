"""Self-talk feature: the organism asks itself a question and answers it —
build_prompt ask/answer branches, self_ask/self_answer with deterministic
fallbacks, and the /self-talk TUI toggle routing the periodic narration."""

import urllib.error
from typing import ClassVar

import pytest
from conftest import patch_generate

from replicanta import llmclient
from replicanta.narration import (
    build_prompt,
    fallback_self_answer,
    fallback_self_ask,
    state_snapshot,
)
from replicanta.organism import BeliefStore, Lifecycle, Metrics
from replicanta.voice import self_answer, self_ask


class _FakeWindow:
    pairs: ClassVar[set] = {("has_fur", "true")}


class _FakeOrg:
    def __init__(self, tmp_path):
        self.store = BeliefStore(tmp_path)
        self.store.chaos = 0.5
        self.store.add(("cat", "has_fur", "true"), 0.9)
        self.lifecycle = Lifecycle(self.store)
        self.window = _FakeWindow()

    def metrics(self):
        return Metrics(self.store)


@pytest.fixture
def org(tmp_path):
    return _FakeOrg(tmp_path)


# -- build_prompt branches -----------------------------------------------------


def test_build_prompt_self_ask_instruction(org):
    prompt = build_prompt(state_snapshot(org), task="self_ask")
    assert "Ask yourself one question" in prompt
    assert "question mark" in prompt


def test_build_prompt_self_answer_includes_question(org):
    prompt = build_prompt(
        state_snapshot(org), task="self_answer", question="why am I here?"
    )
    assert "why am I here?" in prompt
    assert "Answer your own question" in prompt


# -- fallbacks -----------------------------------------------------------------


def test_fallback_self_ask_ends_in_question(org):
    snap = state_snapshot(org)
    q = fallback_self_ask(snap)
    assert q.endswith("?")
    obj = snap["beliefs"][0].split(" ")[1].split("=")[0]
    assert q in {
        f"what do I really believe about {obj}?",
        f"do I still believe what I know about {obj}?",
        f"why do I believe what I know about {obj}?",
    }


def test_fallback_self_ask_empty_beliefs(org):
    org.store.beliefs_map.clear()
    q = fallback_self_ask(state_snapshot(org))
    assert q.endswith("?")
    assert q in {
        "what do I really believe?",
        "what should I believe next?",
        "do I believe anything strongly enough to act on it?",
    }


def test_fallback_self_answer_is_non_meta(org):
    a = fallback_self_answer(state_snapshot(org), "why am I here?")
    assert a and "belief" not in a and "rule" not in a


def test_self_ask_falls_back_when_ollama_down(org, monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("down")

    patch_generate(monkeypatch, boom)
    snap = state_snapshot(org)
    q = self_ask(org)
    assert q.endswith("?")
    obj = snap["beliefs"][0].split(" ")[1].split("=")[0]
    assert q in {
        f"what do I really believe about {obj}?",
        f"do I still believe what I know about {obj}?",
        f"why do I believe what I know about {obj}?",
    }


def test_self_answer_falls_back_when_ollama_down(org, monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("down")

    patch_generate(monkeypatch, boom)
    answer = self_answer(org, "why am I here?")
    assert answer and "belief" not in answer and "rule" not in answer


def test_self_ask_uses_ollama_when_up(org, monkeypatch):
    patch_generate(monkeypatch, lambda *a, **k: "am I more than my beliefs?")
    assert self_ask(org) == "am I more than my beliefs?"


def test_self_ask_skips_ollama_when_voice_offline(org, monkeypatch):
    calls = []
    patch_generate(monkeypatch, lambda *a, **k: calls.append(1))
    llmclient.note_voice_failure()
    llmclient.note_voice_failure()  # streak -> offline
    assert self_ask(org).endswith("?")
    assert calls == []
