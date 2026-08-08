import json
import sys
import urllib.error
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from narration import (
    _ollama_generate,
    build_prompt,
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


def test_build_prompt_includes_snapshot(org):
    prompt = build_prompt(state_snapshot(org))
    assert "wake" in prompt
    assert "cycle 3" in prompt
    assert "has_fur" in prompt
    assert "q1" in prompt


def test_narrate_returns_ollama_response(org, monkeypatch):
    monkeypatch.setattr("narration._ollama_generate",
                        lambda *a, **k: "I wonder about fur.")
    assert narrate(org) == "I wonder about fur."


def test_narrate_falls_back_on_ollama_failure(org, monkeypatch):
    def boom(prompt, model, timeout):
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

    def fake_generate(prompt, model, timeout):
        captured["prompt"] = prompt
        return "Hello, human. I am awake."
    monkeypatch.setattr("narration._ollama_generate", fake_generate)
    reply = respond(org, "hello there")
    assert reply == "Hello, human. I am awake."
    assert "hello there" in captured["prompt"]


def test_respond_falls_back_on_ollama_failure(org, monkeypatch):
    def boom(prompt, model, timeout):
        raise RuntimeError("ollama down")
    monkeypatch.setattr("narration._ollama_generate", boom)
    reply = respond(org, "hello there")
    assert "hello there" in reply
    assert "2 beliefs" in reply
