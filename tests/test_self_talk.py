"""Self-talk feature: the organism asks itself a question and answers it —
build_prompt ask/answer branches, self_ask/self_answer with deterministic
fallbacks, and the /self-talk TUI toggle routing the periodic narration."""

import sys
import urllib.error
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).parent.parent))

import narration
import pytest
from narration import (
    build_prompt,
    fallback_self_answer,
    fallback_self_ask,
    state_snapshot,
)
from organism import BeliefStore, Lifecycle, Metrics
from voice import self_answer, self_ask
from conftest import patch_generate


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
    prompt = build_prompt(state_snapshot(org), self_ask=True)
    assert "Ask yourself one question" in prompt
    assert "question mark" in prompt


def test_build_prompt_self_answer_includes_question(org):
    prompt = build_prompt(state_snapshot(org), self_question="why am I here?")
    assert "why am I here?" in prompt
    assert "Answer your own question" in prompt


# -- fallbacks -----------------------------------------------------------------

def test_fallback_self_ask_ends_in_question(org):
    q = fallback_self_ask(state_snapshot(org))
    assert q.endswith("?")
    assert "believe" in q


def test_fallback_self_ask_empty_beliefs(org):
    org.store.beliefs_map.clear()
    assert fallback_self_ask(state_snapshot(org)) == \
        "what do I really believe?"


def test_fallback_self_answer_echoes_question(org):
    a = fallback_self_answer(state_snapshot(org), "why am I here?")
    assert "why am I here?" in a
    assert "beliefs" in a


def test_self_ask_falls_back_when_ollama_down(org, monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("down")
    patch_generate(monkeypatch, boom)
    q = self_ask(org)
    assert q.endswith("?")
    assert "believe" in q


def test_self_answer_falls_back_when_ollama_down(org, monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("down")
    patch_generate(monkeypatch, boom)
    assert "why am I here?" in self_answer(org, "why am I here?")


def test_self_ask_uses_ollama_when_up(org, monkeypatch):
    patch_generate(monkeypatch, lambda *a, **k: "am I more than my beliefs?")
    assert self_ask(org) == "am I more than my beliefs?"


def test_self_ask_skips_ollama_when_voice_offline(org, monkeypatch):
    calls = []
    patch_generate(monkeypatch, lambda *a, **k: calls.append(1))
    narration.note_voice_failure()
    narration.note_voice_failure()   # streak -> offline
    assert self_ask(org).endswith("?")
    assert calls == []
