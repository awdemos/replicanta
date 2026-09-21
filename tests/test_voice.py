"""Voice-health feature: cached ollama reachability probe, status labels,
failure-streak inference, and the arena's offline fast path."""

import json
import urllib.error
from typing import ClassVar

import pytest
from conftest import patch_generate

from replicanta import llmclient
from replicanta.arena import ThoughtArena
from replicanta.llmclient import probe_voice, voice_online, voice_status
from replicanta.organism import BeliefStore, Lifecycle, Metrics


class _FakeWindow:
    pairs: ClassVar[set] = {("has_fur", "true")}


class _FakeOrg:
    """Minimal organism stand-in: pure-Python store + lifecycle + window."""

    def __init__(self, tmp_path):
        self.dir_path = tmp_path
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


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _tags(monkeypatch, names):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _Resp({"models": [{"name": n} for n in names]}),
    )


def test_probe_offline_when_unreachable(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert probe_voice() is False
    assert voice_online() is False
    assert voice_status() == "offline"


def test_probe_online_when_model_pulled(monkeypatch):
    _tags(monkeypatch, ["qwen2.5:3b", "llama3.2:latest"])
    assert probe_voice("qwen2.5:3b") is True
    assert voice_status() == "online"


def test_probe_online_matches_base_name(monkeypatch):
    _tags(monkeypatch, ["qwen2.5:7b"])
    assert probe_voice("qwen2.5:3b") is True


def test_probe_offline_when_model_missing(monkeypatch):
    _tags(monkeypatch, ["llama3.2:latest"])
    assert probe_voice("qwen2.5:3b") is False


def test_voice_unknown_before_any_probe():
    assert voice_online() is None
    assert voice_status() == "?"


# -- arena fast path ----------------------------------------------------------


def test_arena_skips_debate_when_voice_offline(org, monkeypatch):
    calls = []
    patch_generate(monkeypatch, lambda *a, **k: calls.append(1) or "should not happen")
    llmclient.reset_voice()
    llmclient.note_voice_failure()
    llmclient.note_voice_failure()  # streak -> offline
    text = ThoughtArena().emerge(org)
    assert calls == []
    # Wake-state fallback is intentionally quiet.
    assert text == ""


def test_arena_marks_offline_after_failure_streak(org, monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    patch_generate(monkeypatch, boom)
    ThoughtArena().emerge(org)
    assert voice_online() is None  # one failure: still retryable
    ThoughtArena().emerge(org)
    assert voice_online() is False  # streak: offline


def test_arena_success_clears_offline(org, monkeypatch):
    patch_generate(monkeypatch, lambda *a, **k: "a clear small thought")
    llmclient.note_voice_failure()
    llmclient.note_voice_failure()
    assert voice_online() is False
    llmclient.reset_voice()
    ThoughtArena().emerge(org)
    assert voice_online() is True


def test_respond_records_reply_so_next_prompt_includes_context(org, monkeypatch):
    """voice.respond must record its own reply, otherwise the next
    response prompt has no organism-side history and context decays."""
    replies = ["first reply", "second reply"]
    prompts = []

    def fake_generate(prompt, model, timeout, temperature=0.95):
        prompts.append(prompt)
        return replies.pop(0)

    patch_generate(monkeypatch, fake_generate)
    from replicanta import voice

    assert voice.respond(org, "hello", quick=True) == "first reply"
    assert org.store.chat_log == [["user", "hello"], ["org", "first reply"]]
    assert voice.respond(org, "again", quick=True) == "second reply"
    assert org.store.chat_log[-2:] == [
        ["user", "again"],
        ["org", "second reply"],
    ]
    second_prompt = prompts[-1]
    assert "first reply" in second_prompt
    assert "hello" in second_prompt
    assert "again" in second_prompt


# -- reflection auto-apply gate ------------------------------------------------


_PATCH_PROPOSAL = (
    "patch-extension\n"
    "kind: seed\n"
    "why: regression coverage for the self-modification gate\n"
    "entry: greet the user warmly\n"
)


def _reflect_patch_proposal(monkeypatch, org):
    """Drive voice.reflect to its proposal branch with a canned patch text,
    leaving the real extensions.propose gate under test."""
    from replicanta import voice

    monkeypatch.setattr(ThoughtArena, "emerge", lambda *a, **k: _PATCH_PROPOSAL)
    return voice.reflect(org)


def test_reflect_stages_proposal_when_store_lacks_auto_apply_flag(org, monkeypatch):
    """The self-modification gate fails closed: a store without the
    auto_apply_patches attribute must stage the proposal, never apply it."""
    from replicanta import extensions

    del org.store.auto_apply_patches
    result = _reflect_patch_proposal(monkeypatch, org)
    assert result["action"] == "proposal"
    assert "applied" not in result
    assert extensions.pending() == result["entry"]
    assert extensions.entries() == []


def test_reflect_applies_proposal_only_when_auto_apply_enabled(org, monkeypatch):
    from replicanta import extensions

    org.store.auto_apply_patches = True
    result = _reflect_patch_proposal(monkeypatch, org)
    assert result["action"] == "proposal"
    assert result["applied"] == result["entry"]
    assert extensions.pending() is None
    assert extensions.entries() == [result["entry"]]


# -- transport seam -------------------------------------------------------------
# voice.status/online/probe/mark_offline are the public path to the cached
# voice-health state; consumers must never touch llmclient._voice() directly.


def test_seam_status_and_online_mirror_cached_state():
    from replicanta import voice

    llmclient.reset_voice()
    assert voice.status() == "?"
    assert voice.online() is None
    llmclient.mark_voice_offline()
    assert voice.status() == "offline"
    assert voice.online() is False
    llmclient.reset_voice()


def test_seam_mark_offline_matches_note_failure_streak():
    from replicanta import voice

    llmclient.reset_voice()
    voice.mark_offline()
    assert voice.online() is False
    llmclient.reset_voice()


def test_seam_probe_updates_cached_state(monkeypatch):
    from replicanta import voice

    _tags(monkeypatch, ["qwen2.5:3b"])
    llmclient.reset_voice()
    assert voice.probe("qwen2.5:3b") is True
    assert voice.status() == "online"
    llmclient.reset_voice()


def test_seam_generate_small_defaults_to_default_model(monkeypatch):
    from replicanta import voice

    calls = []

    def fake_generate(prompt, model, timeout=None, temperature=0.95):
        calls.append((prompt, model, timeout, temperature))
        return "go north"

    monkeypatch.setattr("replicanta.llmclient.generate", fake_generate)
    assert voice.generate_small("p", temperature=0.2) == "go north"
    assert calls == [("p", llmclient.DEFAULT_MODEL, None, 0.2)]
    voice.generate_small("p2", model="qwen2.5:3b", timeout=9, temperature=0.7)
    assert calls[-1] == ("p2", "qwen2.5:3b", 9, 0.7)


def test_seam_vision_and_backend_delegate(monkeypatch):
    from replicanta import voice

    monkeypatch.setenv("REPLICANTA_VISION_MODEL", "test-vision")
    assert voice.vision_model() == "test-vision"
    monkeypatch.setenv("REPLICANTA_LLM_BACKEND", "LLAMA_CPP")
    assert voice.llm_backend() == "llama_cpp"


def test_seam_describe_image_and_clean_candidate_delegate(monkeypatch):
    from replicanta import voice

    monkeypatch.setattr("replicanta.llmclient.describe_image", lambda b, **k: "a dark hall")
    assert voice.describe_image(b"bytes") == "a dark hall"
    monkeypatch.setattr("replicanta.llmclient.clean_candidate", lambda t: t.strip())
    assert voice.clean_candidate("  go north  ") == "go north"
