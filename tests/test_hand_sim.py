"""End-to-end test: the real ArmService HTTP client against the simulator
bridge (scripts/sim_hand_bridge.py) — no stubs, traffic over localhost.

Guards both sides of the wire contract: endpoint shapes, SSE state
delivery, error classification, and clean shutdown.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # repo root: scripts/ is not a package on the path

from replicanta.tendon_hand import ArmService, BridgeError
from scripts.sim_hand_bridge import make_server


@pytest.fixture
def bridge():
    server = make_server(port=0, log=lambda line: None)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()
    server.server_close()
    thread.join(timeout=2.0)


def _arm(port):
    return ArmService(bridge_url=f"http://127.0.0.1:{port}")


def test_health_and_state_are_live_against_the_sim(bridge):
    arm = _arm(bridge)
    assert arm.health()["connected"] is True
    state = arm.get_state()
    assert state["connected"] is True
    assert state["simulated"] is True
    assert "index" in state["fingers"]


def test_move_reaches_the_sim_and_state_updates(bridge):
    arm = _arm(bridge)
    assert arm.move("wave", 2.0)["goal"] == "wave"
    assert arm.get_state()["goal"] == "wave"


def test_posture_actuator_and_emotion_accepted(bridge):
    arm = _arm(bridge)
    assert arm.posture("open", 1.0)["posture"] == "open"
    assert arm.actuator("index", "mcp", "flexor", 0.7, 1.0)["ok"] is True
    assert arm.emotion({"stress": 0.4, "arousal": 0.6, "mood": "curious"})["ok"] is True
    emo = arm.get_state()["emotion"]
    assert emo["mood"] == "curious"
    assert emo["stress"] == pytest.approx(0.4)


def test_unknown_move_is_a_value_error_not_a_bridge_error(bridge):
    arm = _arm(bridge)
    with pytest.raises(ValueError, match="unknown move"):
        arm.move("cartwheel")


def test_unreachable_bridge_is_classified_bridge_error():
    arm = ArmService(bridge_url="http://127.0.0.1:1")  # nothing listens on port 1
    with pytest.raises(BridgeError, match="bridge:"):
        arm.move("wave", 1.0)


def test_sse_listener_populates_the_state_cache(bridge):
    arm = _arm(bridge)
    arm.move("fist", 3.0)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if arm._state is not None and arm._state.get("goal"):
            break
        time.sleep(0.1)
    assert arm._state is not None, "SSE never delivered a state frame"
    cached = arm.state()
    assert cached["connected"] is True
    assert cached["goal"] == "fist"
    tensions = [line for line in arm.summary().splitlines() if "tension" in line]
    assert tensions, "summary should carry per-finger tension lines"
    arm.stop()


def test_summary_reads_as_a_live_bridge(bridge):
    arm = _arm(bridge)
    arm.emotion({"stress": 0.5, "arousal": 0.5, "mood": "calm"})
    text = arm.summary()
    assert "hand bridge: live" in text
    assert "50%" in text  # stress readout
