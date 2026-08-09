"""Spoken voice (speech.py): the piper TTS path is optional and contained —
disabled by default, no-ops without a model, serializes utterances on one
daemon thread, and swallows synthesis/playback failures so speech can
never take the organism down. Piper and soundcard are never imported in
these tests: _speak is patched out."""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import speech


def _drain_until_empty(timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if speech._queue.empty():
            return True
        time.sleep(0.01)
    return False


def test_speech_disabled_by_default(monkeypatch):
    called = []
    monkeypatch.setattr(speech, "_speak", lambda text: called.append(text))
    monkeypatch.setattr(speech, "available", lambda: True)
    speech.say("hello")
    assert called == []
    assert speech._queue.empty()


def test_speech_noop_without_model(monkeypatch):
    called = []
    monkeypatch.setattr(speech, "_speak", lambda text: called.append(text))
    monkeypatch.setattr(speech, "available", lambda: False)
    speech.set_enabled(True)
    speech.say("hello")
    assert called == []
    assert speech._queue.empty()


def test_speech_speaks_when_enabled(monkeypatch):
    done = threading.Event()
    said = []

    def fake_speak(text):
        said.append(text)
        if len(said) == 2:
            done.set()

    monkeypatch.setattr(speech, "_speak", fake_speak)
    monkeypatch.setattr(speech, "available", lambda: True)
    speech.set_enabled(True)
    speech.say("first utterance")
    speech.say("second utterance")
    assert done.wait(2.0)
    # one thread, queued in order — utterances never overlap
    assert said == ["first utterance", "second utterance"]


def test_speech_failures_are_contained(monkeypatch):
    done = threading.Event()
    said = []

    def flaky_speak(text):
        if text == "boom":
            raise RuntimeError("piper exploded")
        said.append(text)
        done.set()

    monkeypatch.setattr(speech, "_speak", flaky_speak)
    monkeypatch.setattr(speech, "available", lambda: True)
    speech.set_enabled(True)
    speech.say("boom")
    speech.say("still alive")
    assert done.wait(2.0)
    assert said == ["still alive"]      # failure didn't kill the worker


def test_speech_ignores_empty_text(monkeypatch):
    called = []
    monkeypatch.setattr(speech, "_speak", lambda text: called.append(text))
    monkeypatch.setattr(speech, "available", lambda: True)
    speech.set_enabled(True)
    speech.say("")
    speech.say(None)
    assert called == []
    assert speech._queue.empty()
