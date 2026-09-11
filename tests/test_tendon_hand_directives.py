"""Directive parsing in the tendon-hand Lua module, via the real sandbox."""

import time
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

    def set_decide(self, fn):
        self.decide = fn

    def moves(self):
        return "middle_finger, thumbs_up, reach, grasp, release, point, wave, fist, ripple, pinch, shaka, rock, spock, open, ok"


class _Services:
    def __init__(self, d):
        self._d = d

    def get(self, name):
        return self._d.get(name)

    def register(self, name, svc):
        self._d[name] = svc


class _Hooks:
    def __init__(self):
        self.handlers = {}

    def on(self, ev, fn):
        self.handlers.setdefault(ev, []).append(fn)


class _Events:
    def __init__(self):
        self.handlers = {}
        self.declared = set()
        self.emitted = []

    def declare(self, name):
        self.declared.add(name)

    def on(self, ev, fn):
        self.handlers.setdefault(ev, []).append(fn)

    def emit(self, ev, text=None):
        self.emitted.append((ev, text))
        for fn in self.handlers.get(ev, []):
            fn(text)


class _Commands:
    def register(self, name, fn):
        pass


class _Ctx:
    def __init__(self, services, logs, events):
        self.services = services
        self._logs = logs
        self.events = events

    def log(self, msg):
        self._logs.append(msg)


@pytest.fixture()
def rig():
    arm, hooks, logs = _Arm(), _Hooks(), []
    events = _Events()
    services = _Services({"arm": arm, "commands": _Commands(), "hooks": hooks})
    ctx = _Ctx(services, logs, events)
    lua = build_runtime()
    lua.globals()["_ctx"] = ctx
    lua.execute(MODULE.read_text() + "\ninit(_ctx)")

    def fire(text):
        arm.calls.clear()
        logs.clear()
        for fn in hooks.handlers.get("utterance", []):
            fn(text)
        return list(arm.calls), list(logs)

    fire.arm = arm
    fire.services = services
    fire.events = events
    fire.fire = fire
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
        ("hand: retract the middle finger", "release", 4.0),
        ("hand: retract", "release", 4.0),
        ("hand: relax 2", "release", 2.0),
        ("hand: extend the middle finger", "middle_finger", 4.0),
        ("hand: extend", "open", 4.0),
        ("hand: close your hand", "fist", 4.0),
        ("hand: squeeze 2", "fist", 2.0),
        ("hand: raise it up", "reach", 4.0),
        ("hand: grab the cup", "grasp", 4.0),
        ("hand: wave at the user", "wave", 4.0),
        ("hand: give me the bird", "middle_finger", 4.0),
        ("hand: thumbs up", "thumbs_up", 4.0),
        ("hand: thumbs_up", "thumbs_up", 4.0),
        ("hand: thumb_up", "thumbs_up", 4.0),
        ("hand: give a thumbs up 6", "thumbs_up", 6.0),
        # function-call form: the entity's primary interface
        ('hand.move("wave")', "wave", 4.0),
        ('hand.move("fist", 3)', "fist", 3.0),
        ('hand.move("middle finger")', "middle_finger", 4.0),
        ('hand.move("thumbs up", 6)', "thumbs_up", 6.0),
        ("hand.move('point at me')", "point", 4.0),
        ('Hand.Move("Wave")', "wave", 4.0),
        ('hand.move "wave"', "wave", 4.0),
        ('hand.move("fist 3")', "fist", 3.0),
        ('  hand.move("wave")\nMaking a wave.', "wave", 4.0),
    ],
)
def test_directive_dispatches(rig, text, move, dur):
    calls, _ = rig(text)
    assert calls == [("goal", move, dur)] or calls == [("posture", move, dur)]


def test_posture_call_dispatches_as_posture(rig):
    calls, _ = rig('hand.posture("open", 2)')
    assert calls == [("posture", "open", 2.0)]


def test_only_first_move_per_reply_dispatches(rig):
    # A reply with several move lines (e.g. the model parroting an earlier
    # turn) dispatches only the first — a bridge goal preempts the rest.
    calls, _ = rig('hand.move("wave")\nWaving at you.\nhand.move("thumbs_up")')
    assert calls == [("goal", "wave", 4.0)]


def test_hand_service_registered_and_callable(rig):
    """The module registers a hand API other modules/extensions can call."""
    hand = rig.services.get("hand")
    assert hand is not None
    rig.arm.calls.clear()
    ok = hand["move"]("wave", 2)
    assert ok
    assert rig.arm.calls == [("goal", "wave", 2.0)]
    assert "wave" in hand["moves"]()
    assert hand["move"]("cartwheel") is False
    assert rig.arm.calls == [("goal", "wave", 2.0)]  # cartwheel did not dispatch


@pytest.mark.parametrize(
    "text",
    [
        "she said hand: wave",  # directive must start the line
        "hand:5",  # no move word
        "handwriting: fist",  # not the directive marker
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


def test_explicit_goal_holds_off_volition():
    """An explicit move sets a hold so volition can't stomp it mid-move."""
    from replicanta.tendon_hand import ArmService

    arm = ArmService()
    sent = []
    arm._post = lambda path, body: sent.append((path, body))

    assert arm._explicit_hold_until == 0.0
    arm.goal("wave", 4.0)
    assert sent == [("/goal", {"kind": "wave", "duration_s": 4.0})]
    hold = arm._explicit_hold_until
    assert hold > time.time() + 4.0  # move duration + grace
    # volitional goals go through but don't extend the hold
    arm.goal("fist", 6.0, _volitional=True)
    assert arm._explicit_hold_until == hold


def test_arm_service_accepts_lua_tables():
    """Lua passes lupa tables, not dicts; pose/emotion must accept them."""
    from replicanta.lua_sandbox import build_runtime
    from replicanta.tendon_hand import ArmService

    lua = build_runtime()
    arm = ArmService()
    sent = []
    arm._post = lambda path, body: sent.append((path, body))

    arm.emotion(lua.execute("return {stress = 0.25, arousal = 0.55}"))
    assert sent == [("/emotion", {"stress": 0.25, "arousal": 0.55})]

    sent.clear()
    arm.pose(lua.execute("return {fingers = {index = 0.5, thumb = 0.1}, duration_s = 2.0}"))
    assert sent == [("/pose", {"fingers": {"index": 0.5, "thumb": 0.1}, "duration_s": 2.0})]

    # plain dicts keep working (TUI/web dispatch path)
    sent.clear()
    arm.emotion({"stress": 0.9, "mood": "calm"})
    assert sent == [("/emotion", {"stress": 0.9, "mood": "calm"})]


def test_module_uses_service_provided_moves():
    """init.lua builds its move vocabulary from arm:moves() when the
    service provides one, so the whitelist has a single source of truth."""
    from replicanta.lua_sandbox import build_runtime

    class _VocabArm:
        def __init__(self):
            self.calls = []

        def moves(self):
            return "flutter, wave"  # service-side vocabulary

        def goal(self, kind, dur):
            if kind not in ("flutter", "wave"):
                raise ValueError(f"unknown goal {kind!r}")
            self.calls.append(("goal", kind, dur))
            return {"ok": True}

        def posture(self, name, dur):
            if name not in ("flutter", "wave"):
                raise ValueError(f"unknown posture {name!r}")
            self.calls.append(("posture", name, dur))
            return {"ok": True}

    arm, hooks, logs = _VocabArm(), _Hooks(), []
    services = _Services({"arm": arm, "commands": _Commands(), "hooks": hooks})
    ctx = _Ctx(services, logs, _Events())
    lua = build_runtime()
    lua.globals()["_ctx"] = ctx
    lua.execute(MODULE.read_text() + "\ninit(_ctx)")
    hand = services.get("hand")
    assert "flutter" in hand["moves"]()
    assert hand["move"]("flutter", 2) is True
    assert arm.calls == [("goal", "flutter", 2.0)]


def test_real_module_learned_hook_delivers_emotion(tmp_path):
    """Regression: the real init.lua wraps arm:emotion in pcall, so a
    conversion error is silently swallowed — the payload must actually
    reach the bridge."""
    import shutil

    from replicanta.modules import ModuleLoader
    from replicanta.tendon_hand import ArmService

    src = Path(__file__).resolve().parent.parent / "modules"
    shutil.copytree(src, tmp_path / "modules")
    loader = ModuleLoader(
        tmp_path / "modules",
        organism=None,
        config={"modules": {"enabled": ["base", "tendon-hand"]}},
    )
    loader.load_all()
    assert "tendon-hand" in loader.modules
    assert not any("tendon-hand" in w for w in loader.warnings)

    arm = loader.registry.get("arm")
    assert isinstance(arm, ArmService)
    sent = []
    arm._post = lambda path, body: sent.append((path, body))
    loader.registry.get("hooks").emit("learned", "something new")
    assert ("/emotion", {"stress": 0.25, "arousal": 0.55}) in sent


def test_module_installs_lua_decide_policy(rig):
    assert hasattr(rig.arm, "decide") and rig.arm.decide is not None
    # calm + low stress -> wave; high stress -> fist; tired -> release
    assert rig.arm.decide({"mood": "calm", "stress": 0.1, "arousal": 0.1, "chaos": 0.1, "insane": False}) == "wave"
    assert rig.arm.decide({"mood": "calm", "stress": 0.9, "arousal": 0.1, "chaos": 0.1, "insane": False}) == "fist"
    assert rig.arm.decide({"mood": "tired", "stress": 0.1, "arousal": 0.1, "chaos": 0.1, "insane": False}) == "release"


def test_module_declares_and_emits_hand_events(rig):
    assert {"hand_goal", "hand_error"} <= rig.events.declared
    rig.fire('hand.move("wave", 3)')
    assert ("hand_goal", "wave") in rig.events.emitted
