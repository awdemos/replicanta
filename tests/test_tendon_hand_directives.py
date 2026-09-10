"""Directive parsing in the tendon-hand Lua module, via the real sandbox."""

from pathlib import Path

import pytest

from replicanta.lua_sandbox import build_runtime
from replicanta.tendon_hand import GOALS, POSTURES

MODULE = Path(__file__).resolve().parent.parent / "modules" / "tendon-hand" / "init.lua"


class _Arm:
    def __init__(self):
        self.calls = []

    def goal(self, kind, dur):
        if kind not in GOALS:
            raise ValueError(f"unknown goal {kind!r}")
        self.calls.append(("goal", kind, dur))
        return {"ok": True}

    def posture(self, name, dur):
        if name not in POSTURES:
            raise ValueError(f"unknown posture {name!r}")
        self.calls.append(("posture", name, dur))
        return {"ok": True}


class _Services:
    def __init__(self, d):
        self._d = d

    def get(self, name):
        return self._d.get(name)


class _Hooks:
    def __init__(self):
        self.handlers = {}

    def on(self, ev, fn):
        self.handlers.setdefault(ev, []).append(fn)


class _Commands:
    def register(self, name, fn):
        pass


class _Ctx:
    def __init__(self, services, logs):
        self.services = services
        self._logs = logs

    def log(self, msg):
        self._logs.append(msg)


@pytest.fixture()
def rig():
    arm, hooks, logs = _Arm(), _Hooks(), []
    ctx = _Ctx(_Services({"arm": arm, "commands": _Commands(), "hooks": hooks}), logs)
    lua = build_runtime()
    lua.globals()["_ctx"] = ctx
    lua.execute(MODULE.read_text() + "\ninit(_ctx)")

    def fire(text):
        arm.calls.clear()
        logs.clear()
        for fn in hooks.handlers.get("utterance", []):
            fn(text)
        return list(arm.calls), list(logs)

    return fire


@pytest.mark.parametrize(
    ("text", "move", "dur"),
    [
        ("hand: wave", "wave", 4.0),
        ("hand: fist 3", "fist", 3.0),
        ("hand: middle finger", "middle_finger", 4.0),
        ("hand: middle finger 6", "middle_finger", 6.0),
        ("hand: middle_finger", "middle_finger", 4.0),
        ("Hand: Middle Finger", "middle_finger", 4.0),
        ("hand: flip off", "middle_finger", 4.0),
        ("[hand: point at me", "point", 4.0),
        ("hand: point at me and hold it for 8 seconds", "point", 8.0),
        ("hand: open 2\nsome prose", "open", 2.0),
        ("hand: okay then", "ok", 4.0),
    ],
)
def test_directive_dispatches(rig, text, move, dur):
    calls, _ = rig(text)
    assert calls == [("goal", move, dur)] or calls == [("posture", move, dur)]


@pytest.mark.parametrize(
    "text",
    [
        "she said hand: wave",   # directive must start the line
        "hand:5",                # no move word
        "handwriting: fist",     # not the directive marker
        "just prose, no directive",
    ],
)
def test_non_directives_do_not_dispatch(rig, text):
    calls, _ = rig(text)
    assert calls == []


def test_unknown_move_falls_back_to_first_word(rig):
    calls, logs = rig("hand: cartwheel")
    assert calls == []
    assert any("unknown move 'cartwheel'" in line for line in logs)
