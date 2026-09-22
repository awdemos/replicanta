"""Service-level tests for the ArmService capability bridge."""

import pytest

from replicanta.tendon_hand import ArmService, BridgeError, GOALS, POSTURES


def _stub_request(service, payloads):
    """Capture HTTP traffic: payloads.get(path) -> response dict or Exception."""
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        result = payloads.get(path)
        if isinstance(result, Exception):
            raise result
        return result if result is not None else {}

    service._request = fake
    return calls


def test_health_reports_ok_from_bridge():
    arm = ArmService()
    calls = _stub_request(arm, {"/healthz": {"ok": True}})
    result = arm.health()
    assert result.ok is True
    assert result.connected is True
    assert calls == [("GET", "/healthz", None)]


def test_health_reports_offline_on_error():
    arm = ArmService()
    _stub_request(arm, {"/healthz": ConnectionRefusedError("no bridge")})
    result = arm.health()
    assert result["connected"] is False
    assert "no bridge" in result["error"]


def test_state_uses_cached_sse_snapshot_without_http():
    arm = ArmService()
    calls = _stub_request(arm, {})  # any HTTP call would be recorded
    arm._state = {"goal": "wave", "fingers": {"index": {"joints": {}}}, "emotion": {"mood": "calm"}}
    state = arm.state()
    assert state.goal == "wave"
    assert state.emotion.mood == "calm"
    assert calls == []  # cache hit: no HTTP


def test_state_falls_back_to_http_before_first_sse_frame():
    arm = ArmService()
    calls = _stub_request(arm, {"/arm": {"goal": "fist", "fingers": {}}})
    state = arm.state()
    assert state.goal == "fist"
    assert calls == [("GET", "/arm", None)]


def test_telemetry_returns_recent_samples():
    arm = ArmService()
    arm._telemetry_log = [(1.0, "index_tension", 0.5), (1.0, "goal", "wave")]
    assert arm.telemetry() == [(1.0, "index_tension", 0.5), (1.0, "goal", "wave")]
    arm.telemetry().clear()  # a copy: mutating it must not drain the service
    assert len(arm.telemetry()) == 2


def test_health_reports_malformed_payload_as_offline():
    arm = ArmService()
    _stub_request(arm, {"/healthz": ["not", "a", "dict"]})
    result = arm.health()
    assert result["connected"] is False
    assert "/healthz" in result["error"]


def test_state_cache_hit_reports_connected():
    arm = ArmService()
    _stub_request(arm, {})
    arm._state = {"goal": "wave", "fingers": {}}
    assert arm.state().connected is True


def test_state_cold_path_failure_reports_offline():
    arm = ArmService()
    _stub_request(arm, {"/arm": ConnectionRefusedError("down")})
    state = arm.state()
    assert state.connected is False
    assert "down" in state.error


def test_set_decide_overrides_volition_choice():
    arm = ArmService()
    arm.set_decide(lambda inputs: "wave" if inputs["stress"] < 0.5 else "fist")
    assert arm._choose("calm", 0.1, 0.1, 0.1, False) == "wave"
    assert arm._choose("calm", 0.9, 0.1, 0.1, False) == "fist"


def test_set_decide_none_restores_default_policy():
    arm = ArmService()
    arm.set_decide(lambda _inputs: "wave")
    arm.set_decide(None)
    # default policy: high stress -> protective fist
    assert arm._choose("calm", 0.9, 0.0, 0.0, False) == "fist"


def test_decide_fn_receiving_dict_returns_none_for_no_move():
    arm = ArmService()
    arm.set_decide(lambda _inputs: None)
    assert arm._choose("calm", 0.1, 0.1, 0.1, False) is None


def test_decide_fn_gets_live_state_snapshot():
    arm = ArmService()
    seen = []
    arm.set_decide(lambda inputs: seen.append(inputs.get("state")) or "point")
    arm._state = {"goal": "reach"}
    arm._choose("calm", 0.1, 0.1, 0.1, False)
    assert seen == [{"goal": "reach"}]


def test_decide_call_uses_configured_lock():
    import threading

    arm = ArmService(lua_lock=threading.Lock())
    with arm._lua_lock:
        arm.set_decide(lambda _inputs: "ok" if arm._lua_lock.locked() else "unlocked")
    assert arm._choose("calm", 0.1, 0.1, 0.1, False) == "ok"


def test_volition_tick_survives_raising_decide_fn():
    arm = ArmService()
    arm.set_decide(lambda _inputs: 1 / 0)
    assert arm._volition_tick("calm", 0.1, 0.1, 0.1, False, 1000.0) is None
    assert arm._last_volition == 1000.0  # cadence advances: no 0.5s hot loop


def test_volition_tick_recovers_after_raising_fn():
    arm = ArmService()
    arm.set_decide(lambda _inputs: 1 / 0)
    assert arm._volition_tick("calm", 0.1, 0.1, 0.1, False, 1000.0) is None
    arm.set_decide(lambda _inputs: "wave")
    assert arm._volition_tick("calm", 0.1, 0.1, 0.1, False, 1010.0) == "wave"
    assert arm._last_volition == 1010.0


def test_volition_tick_updates_cadence_when_move_repeats():
    arm = ArmService()
    arm.set_decide(lambda _inputs: "wave")
    arm._last_move = "wave"  # policy keeps choosing the same move
    assert arm._volition_tick("calm", 0.1, 0.1, 0.1, False, 1000.0) == "wave"
    assert arm._last_volition == 1000.0


def test_volition_tick_updates_cadence_on_no_move():
    arm = ArmService()
    arm.set_decide(lambda _inputs: None)
    assert arm._volition_tick("calm", 0.1, 0.1, 0.1, False, 1000.0) is None
    assert arm._last_volition == 1000.0


def test_volition_loop_survives_raising_policy():
    class _Store:
        def belief_value(self, _obj, _attr, default):
            return "calm"

    org = type("Org", (), {"store": _Store()})()
    arm = ArmService(organism=org)

    def boom(_inputs):
        arm._alive = False  # stop the loop after this tick
        raise RuntimeError("policy exploded")

    arm.set_decide(boom)
    arm._volition_loop()  # must return without propagating the policy error
    assert arm._alive is False  # proves the loop reached and ran the tick


def test_volition_loop_survives_non_numeric_store_values():
    """Regression: a string/None store value used to raise inside the loop
    body and kill the volition thread for the whole session."""

    class _WeirdStore:
        def belief_value(self, _obj, _attr, default):
            return "calm"

        stress = "high"
        arousal = "hot"
        chaos = None
        insane = "yes"

    org = type("Org", (), {"store": _WeirdStore()})()
    arm = ArmService(organism=org)
    arm._request = lambda *a, **k: {}

    def decide(_inputs):
        arm._alive = False  # one tick, then let the loop exit
        return "wave"

    arm.set_decide(decide)
    arm._volition = True  # without starting real threads, as below
    arm._volition_loop()  # must not raise despite the weird store values
    assert arm._last_volition > 0.0  # cadence advanced: the tick ran
    assert arm._last_move == "wave"  # and the move actually dispatched


def test_volition_thread_stays_alive_with_bad_store_values():
    """End-to-end: with the real background thread, malformed store values
    must not let the volition thread die."""
    import time as _time

    class _WeirdStore:
        def belief_value(self, _obj, _attr, default):
            return "calm"

        stress = "high"
        arousal = "hot"
        chaos = None
        insane = False

    org = type("Org", (), {"store": _WeirdStore()})()
    arm = ArmService(organism=org)
    arm._request = lambda *a, **k: {}
    arm.volition(True)  # starts the real threads
    _time.sleep(1.5)  # several ticks at 0.5 s
    assert arm._vol_thread is not None and arm._vol_thread.is_alive()
    arm.stop()


def test_goals_and_postures_csvs():
    arm = ArmService()
    assert set(arm.goals().split(", ")) == set(GOALS)
    assert set(arm.postures().split(", ")) == set(POSTURES)


def test_bridge_error_distinguished_from_validation():
    """Whitelist misses keep raising plain ValueError; bridge/network
    failures raise BridgeError with the "bridge:" prefix Lua keys on."""
    arm = ArmService()

    def down(method, path, data, headers):
        raise ConnectionResetError("reset")

    arm._roundtrip = down
    with pytest.raises(BridgeError) as info:
        arm.move("wave", 2.0)
    assert str(info.value).startswith("bridge:")
    with pytest.raises(ValueError, match="unknown move"):
        arm.move("cartwheel", 2.0)  # validated before any network I/O


def test_move_retries_once_on_connection_refused():
    """A bridge mid-restart costs one bounded retry, then success."""
    arm = ArmService()
    calls = []

    def flaky(method, path, data, headers):
        calls.append(path)
        if len(calls) == 1:
            raise ConnectionRefusedError("restarting")
        return {"ok": True}

    arm._roundtrip = flaky
    result = arm.move("wave", 2.0)
    assert result["goal"] == "wave"
    assert calls == ["/goal", "/goal"]


def test_move_fails_fast_on_timeout():
    """A hung bridge must not be retried: fail after a single attempt."""
    arm = ArmService()
    calls = []

    def hung(method, path, data, headers):
        calls.append(path)
        raise TimeoutError("hung")

    arm._roundtrip = hung
    with pytest.raises(BridgeError, match="bridge:"):
        arm.move("wave", 2.0)
    assert calls == ["/goal"]


def test_stopped_service_rejects_moves():
    arm = ArmService()
    arm.stop()
    with pytest.raises(RuntimeError, match="stopped"):
        arm.move("wave", 1.0)
    with pytest.raises(RuntimeError, match="stopped"):
        arm.posture("open", 1.0)
    with pytest.raises(RuntimeError, match="stopped"):
        arm.emotion({"stress": 0.5})
