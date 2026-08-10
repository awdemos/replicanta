"""Group chat: several organisms share one transcript; a user line is
broadcast to every member (or just the addressed one), each speaker
replies through its own arena in quick mode, and utterances persist in
each speaker's episodic memory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import groupchat
from groupchat import GroupChat
from organism import BeliefStore, Lifecycle, Metrics
from conftest import patch_generate


class _Window:
    def __init__(self):
        self.pairs = {("has_fur", "true")}


def _org(tmp_path, name):
    class _Org:
        def __init__(self, dir_path):
            self.store = BeliefStore(dir_path)
            self.store.cycle = 3
            self.store.chaos = 0.5
            self.lifecycle = Lifecycle(self.store)
            self.window = _Window()

        def metrics(self):
            return Metrics(self.store)

    org = _Org(tmp_path / name)
    org.store.add(("cat", "has_fur", "true"), 0.9)
    return org


@pytest.fixture
def group(tmp_path):
    return GroupChat({
        "fern": _org(tmp_path, "fern"),
        "willow": _org(tmp_path, "willow"),
    })


def _scripted(monkeypatch, replies):
    """Each member's quick take returns its own scripted reply; records
    the number of ollama calls."""
    calls = []

    def fake(prompt, model, timeout, temperature=0.95):
        calls.append(prompt)
        return replies.pop(0)

    patch_generate(monkeypatch, fake)
    return calls


def test_needs_two_members(tmp_path):
    with pytest.raises(ValueError, match="at least two"):
        GroupChat({"solo": _org(tmp_path, "solo")})


def test_broadcast_round_robin_in_speaking_order(group, monkeypatch):
    calls = _scripted(monkeypatch, ["hi from fern", "hi from willow"])
    utterances = group.broadcast("hello everyone")
    assert utterances == [("fern", "hi from fern"),
                          ("willow", "hi from willow")]
    # quick mode: exactly one ollama call per speaker
    assert len(calls) == 2
    speakers = [s for s, _ in group.transcript]
    assert speakers == ["user", "fern", "willow"]


def test_addressed_member_replies_alone(group, monkeypatch):
    calls = _scripted(monkeypatch, ["you called?"])
    utterances = group.broadcast("willow: what do you think?")
    assert utterances == [("willow", "you called?")]
    assert len(calls) == 1


def test_at_address_syntax(group, monkeypatch):
    _scripted(monkeypatch, ["present"])
    utterances = group.broadcast("@fern are you there")
    assert utterances == [("fern", "present")]


def test_context_names_the_roster_and_recent_lines(group):
    group._append("user", "hello everyone")
    group._append("fern", "hi from fern")
    ctx = group.context()
    assert "fern, willow" in ctx
    assert "user: hello everyone" in ctx
    assert "fern: hi from fern" in ctx


def test_utterances_persist_in_speaker_memory(group, monkeypatch):
    _scripted(monkeypatch, ["hi from fern", "hi from willow"])
    group.broadcast("hello everyone")
    fern_said = [e["text"] for e in group.members["fern"].store.memory
                 if e["kind"] == "group"]
    assert any("I said: hi from fern" in t for t in fern_said)
    # every member also remembers the user's line
    for org in group.members.values():
        heard = [e["text"] for e in org.store.memory
                 if e["kind"] == "group"]
        assert any("user: hello everyone" in t for t in heard)


def test_transcript_capped(group):
    for i in range(groupchat.MAX_TRANSCRIPT + 10):
        group._append("user", f"line {i}")
    assert len(group.transcript) == groupchat.MAX_TRANSCRIPT
    assert group.transcript[0][1] == "line 10"


def test_full_debate_opt_in(group, monkeypatch):
    """quick=False runs the whole five-call arena per speaker."""
    script = (["fur and paws", "fur and quiet", "both weak",
               "VOTE: 1", "VOTE: 1"]) * 2
    calls = _scripted(monkeypatch, script)
    group.broadcast("hello everyone", quick=False)
    assert len(calls) == 10
