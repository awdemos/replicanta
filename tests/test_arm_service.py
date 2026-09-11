"""Service-level tests for the ArmService capability bridge."""

from replicanta.tendon_hand import ArmService


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
    assert arm.health() == {"ok": True, "connected": True}
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
