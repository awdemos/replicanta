"""Shared test fixtures: the cached ollama voice state, the spoken-voice
state and the extension registry are module-global, so reset them around
every test to keep reachability, speech and registry deterministic."""

import pytest

from replicanta import extensions, llmclient, speech


def patch_generate(monkeypatch, fake):
    """Patch the llmclient generation seam with a text-returning fake.

    Arena calls llmclient.generate_with_stats, which returns
    (text, stats); most tests only care about the text, so this wraps a
    classic str-returning fake and supplies zeroed token stats. Fakes
    that raise still raise. Tests asserting metered token counts should
    patch generate_with_stats directly with their own stats dict."""

    def wrapper(*a, **k):
        return fake(*a, **k), {"prompt_tokens": 0, "gen_tokens": 0}

    monkeypatch.setattr("replicanta.llmclient.generate_with_stats", wrapper)


@pytest.fixture(autouse=True)
def _reset_voice_state():
    llmclient.reset_voice()
    extensions.reset()
    speech.reset()
    yield
    llmclient.reset_voice()
    extensions.reset()
    speech.reset()
