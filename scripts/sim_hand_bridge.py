#!/usr/bin/env python3
"""Simulator for the tendon-hand bridge (robot-hand/bridge/server.py).

Implements the bridge HTTP contract that src/replicanta/tendon_hand.py
(ArmService) speaks, so the full pipeline — entity utterance -> Lua
module -> arm service -> bridge -> hand — can run and be observed without
the physical hand:

    GET  /healthz        -> {"ok": true, ...}
    GET  /arm            -> {"goal", "emotion": {mood, stress, arousal},
                             "fingers": {name: {"joints": {joint: {
                               "flex_tension", "flex_force", "strain"}}}}}
    POST /goal           {"kind", "duration_s"}
    POST /posture        {"name", "duration_s"}
    POST /actuator       {"finger", "joint", "side", "activation", "duration_s"}
    POST /pose           {joint-table spec, "duration_s"}
    POST /emotion        {"stress"?, "arousal"?, "mood"?}
    GET  /events         -> SSE stream of {"type": "arm", "state": ...}

Every accepted command is appended to a log (stdout by default) so the
gestures can be watched: `[12:00:01] GOAL wave 4.0s`.

Usage:

    python3 scripts/sim_hand_bridge.py [--port 8765] [--log PATH] [--quiet]

Stdlib only. The simulated tendons ramp toward the active goal's targets
and relax to rest afterwards; the numbers are plausible, not physical.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FINGERS = ("thumb", "index", "middle", "ring", "pinky")
JOINTS = ("mcp", "pip", "dip")

# Approximate per-goal tendon activation targets (0..1) for the sim.
_GOAL_TARGETS = {
    "reach": 0.25,
    "grasp": 0.75,
    "release": 0.05,
    "point": 0.2,
    "wave": 0.35,
    "fist": 0.95,
    "ripple": 0.5,
    "pinch": 0.55,
    "ok": 0.45,
    "shaka": 0.3,
    "rock": 0.6,
    "spock": 0.4,
    "middle_finger": 0.5,
    "thumbs_up": 0.5,
}


class SimState:
    """The fake hand: emotion, active goal, and per-tendon activation."""

    def __init__(self, log):
        self._lock = threading.Lock()
        self._log = log
        self.goal = None
        self.goal_until = 0.0
        self.emotion = {"mood": "calm", "stress": 0.0, "arousal": 0.0}
        # activation per (finger, joint, side); the sim animates toward targets
        self._act = {(f, j, side): 0.0 for f in FINGERS for j in JOINTS for side in ("flexor", "extensor")}
        self._seq = 0
        self._subscribers = []

    # -- command handling -------------------------------------------------------
    def command(self, path, payload):
        """Apply one POST; returns the new goal name or None."""
        now = time.time()
        with self._lock:
            if path == "/emotion":
                for key in ("stress", "arousal"):
                    if key in payload:
                        self.emotion[key] = max(0.0, min(1.0, float(payload[key])))
                if payload.get("mood"):
                    self.emotion["mood"] = str(payload["mood"])
                self._record(f"EMOTION {json.dumps(self.emotion, sort_keys=True)}")
            elif path in ("/goal", "/posture"):
                kind = str(payload.get("kind") or payload.get("name") or "").lower()
                dur = float(payload.get("duration_s", 4.0))
                self.goal = kind
                self.goal_until = now + max(0.2, dur)
                self._record(f"{'GOAL' if path == '/goal' else 'POSTURE'} {kind} {dur:.1f}s")
            elif path == "/actuator":
                finger = str(payload.get("finger", "index"))
                joint = str(payload.get("joint", "mcp"))
                side = str(payload.get("side", "flexor"))
                act = max(0.0, min(1.0, float(payload.get("activation", 0.5))))
                dur = float(payload.get("duration_s", 4.0))
                self._act[(finger, joint, side)] = act
                self.goal = "actuator"
                self.goal_until = now + max(0.2, dur)
                self._record(f"ACTUATOR {finger}/{joint} {side}={act:.2f} {dur:.1f}s")
            elif path == "/pose":
                self.goal = "pose"
                self.goal_until = now + float(payload.get("duration_s", 2.0))
                self._record(f"POSE {json.dumps(payload, sort_keys=True)}")
            else:
                return None
        self.broadcast()
        return self.goal

    # -- state + physics ----------------------------------------------------------
    def _record(self, line):
        self._seq += 1
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        self._log(f"[{stamp}] {line}")

    def snapshot(self):
        with self._lock:
            now = time.time()
            active = self.goal if now < self.goal_until else None
            if active is None and self.goal is not None:
                self.goal = None  # the gesture has played out
            target = _GOAL_TARGETS.get(active, 0.0)
            fingers = {}
            for f in FINGERS:
                joints = {}
                for j in JOINTS:
                    cur = self._act[(f, j, "flexor")]
                    # ramp toward the goal target and relax when idle
                    want = target if active else 0.0
                    nxt = cur + (want - cur) * 0.35
                    self._act[(f, j, "flexor")] = nxt
                    ext = self._act[(f, j, "extensor")]
                    self._act[(f, j, "extensor")] = ext + (0.0 - ext) * 0.2
                    joints[j] = {
                        "flex_tension": round(nxt * 42.0, 2),  # N, plausible-looking
                        "flex_force": round(nxt * 18.0, 2),
                        "strain": round(nxt * 0.6, 3),
                    }
                fingers[f] = {"joints": joints}
            return {
                "goal": active,
                "emotion": dict(self.emotion),
                "fingers": fingers,
                "simulated": True,
            }

    # -- SSE ----------------------------------------------------------------------
    def subscribe(self):
        queue = []
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue):
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def broadcast(self):
        state = self.snapshot()
        event = json.dumps({"type": "arm", "state": state})
        with self._lock:
            subs = list(self._subscribers)
        for queue in subs:
            queue.append(event)


class _Handler(BaseHTTPRequestHandler):
    server_version = "SimTendonBridge/1.0"

    @property
    def sim(self):
        return self.server.sim_state

    def log_message(self, *args):  # quiet by default; commands are logged by SimState
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json({"ok": True, "simulated": True, "ts": time.time()})
        elif self.path == "/arm":
            self._send_json(self.sim.snapshot())
        elif self.path == "/events":
            self._sse()
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        if self.path in ("/goal", "/posture", "/actuator", "/pose", "/emotion"):
            self.sim.command(self.path, self._read_json())
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, status=404)

    def _sse(self):
        """SSE stream: immediate snapshot, then a state push every 0.5 s
        (plus whatever broadcasts the commands trigger)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        queue = self.sim.subscribe()
        seq = 0
        try:
            self._write_event(seq, json.dumps({"type": "arm", "state": self.sim.snapshot()}))
            seq += 1
            while True:
                time.sleep(0.5)
                while queue:
                    self._write_event(seq, queue.pop(0))
                    seq += 1
                # heartbeat keeps tension numbers moving even when idle
                self._write_event(seq, json.dumps({"type": "arm", "state": self.sim.snapshot()}))
                seq += 1
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.sim.unsubscribe(queue)

    def _write_event(self, seq, data):
        self.wfile.write(f"id: {seq}\ndata: {data}\n\n".encode())
        self.wfile.flush()


def make_server(port=8765, log=None):
    """Build (but do not start) a simulator on the given port."""
    sim = SimState(log or (lambda line: print(line, flush=True)))
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.sim_state = sim
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Simulate the tendon-hand bridge")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", default=None, help="command log path (default: stdout)")
    parser.add_argument("--quiet", action="store_true", help="suppress the command log")
    args = parser.parse_args(argv)

    if args.quiet:
        sink = lambda line: None  # noqa: E731
    elif args.log:
        fh = open(args.log, "a", buffering=1)  # noqa: SIM115

        def sink(line, fh=fh):
            fh.write(line + "\n")

    else:
        sink = lambda line: print(line, flush=True)  # noqa: E731

    server = make_server(args.port, sink)
    print(f"sim-hand-bridge: listening on 127.0.0.1:{args.port} (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
