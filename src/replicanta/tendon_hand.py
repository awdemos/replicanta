"""Tendon-hand bridge service for Replicanta organisms.

Runs an SSE listener thread against the local robot-hand bridge and exposes
arm control to Lua modules/hooks as the ``arm`` service.
"""

import contextlib
import json
import logging
import threading
import time
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlparse

from replicanta import lua_sandbox

log = logging.getLogger(__name__)

GOALS = (
    "reach",
    "grasp",
    "release",
    "point",
    "wave",
    "fist",
    "ripple",
    "pinch",
    "ok",
    "shaka",
    "rock",
    "spock",
    "middle_finger",
    "thumbs_up",
)

POSTURES = (
    "open",
    "fist",
    "pinch",
    "ok",
    "point",
    "shaka",
    "rock",
    "spock",
    "ripple",
    "reach",
    "grasp",
    "release",
    "wave",
    "middle_finger",
    "thumbs_up",
)

# Everything the hand can be told to do: GOALS plus the "open" posture.
# Multi-word names come first so phrase matching prefers them.
MOVES = (
    "middle_finger",
    "thumbs_up",
    "reach",
    "grasp",
    "release",
    "point",
    "wave",
    "fist",
    "ripple",
    "pinch",
    "shaka",
    "rock",
    "spock",
    "open",
    "ok",
)


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(x)))


class ArmService:
    """Client for the tendon-hand bridge with optional volitional control.

    Exposed to Lua as ``services.get('arm')`` with methods:
      get_state(), state(), health(), telemetry(), goal(kind, duration),
      posture(name, duration), actuator(finger, joint, side, activation),
      pose(spec), emotion(spec), summary(), moves(), postures(),
      volition(enabled), set_decide(fn), dispatch(args).

    The SSE listener and volition threads start lazily on first use.
    When volition is enabled (default), a background thread reads the
    organism's mood/stress/arousal every few seconds and sends goals to the
    hand without human input.
    """

    def __init__(self, organism=None, bridge_url="http://127.0.0.1:8765", tick_hz=2.0, lua_lock=None):
        self.organism = organism
        self._url = urlparse(bridge_url)
        self._host = self._url.hostname or "127.0.0.1"
        self._port = self._url.port or 8765
        self._lock = threading.Lock()
        self._lua_lock = lua_lock if lua_lock is not None else threading.Lock()
        self._decide_fn = None  # Lua-installed volition policy (see set_decide)
        self._state = None
        self._volition = True
        self._last_volition = 0.0
        self._last_goal = None
        # explicit moves (chat requests, slash commands) hold off volitional
        # ones until the move has played out, so autonomy can't stomp them
        self._explicit_hold_until = 0.0
        self._telemetry_log = []  # recent (time, key, value) tuples
        self._alive = True
        # Threads start lazily on first real use: ModuleLoader constructs an
        # ArmService per organism, and eager start would spawn SSE/volition
        # loops (and live bridge connections) for organisms that never move.
        self._thread = None
        self._vol_thread = None

    def _ensure_started(self):
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._sse_loop, daemon=True)
            self._thread.start()
            self._vol_thread = threading.Thread(target=self._volition_loop, daemon=True)
            self._vol_thread.start()

    # -- public API used by Lua ------------------------------------------------
    def get_state(self):
        try:
            raw = self._get_json("/arm")
            raw.setdefault("connected", True)
        except Exception as exc:  # noqa: BLE001
            raw = {"error": str(exc), "connected": False, "fingers": {}}
        # Wrap the dict in an object so Lua can read attributes via the
        # sandbox's attribute getter (dicts are not attribute-accessible).
        return _DictProxy(raw)

    def health(self):
        """Bridge liveness probe; never raises (offline -> connected=false)."""
        try:
            raw = self._get_json("/healthz")
            if not isinstance(raw, dict):
                raise TypeError(f"/healthz returned {type(raw).__name__}")
            raw.setdefault("connected", True)
            return raw
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "connected": False, "error": str(exc)}

    def state(self):
        """Latest hand state: the SSE-cached snapshot when available, else
        a blocking HTTP fetch (only before the first SSE frame arrives)."""
        if self._state is not None:
            return _DictProxy({**self._state, "connected": True})
        return self.get_state()

    def telemetry(self):
        """Recent (time, key, value) samples recorded from SSE frames."""
        with self._lock:
            return list(self._telemetry_log)

    def goal(self, kind, duration_s=4.0, _volitional=False):
        kind = str(kind).lower()
        if kind not in GOALS:
            raise ValueError(f"unknown goal {kind!r}; try {GOALS}")
        duration_s = max(0.3, min(15.0, float(duration_s)))
        self._post("/goal", {"kind": kind, "duration_s": duration_s})
        if not _volitional:
            self._explicit_hold_until = time.time() + duration_s + 2.0
        return {"ok": True, "goal": kind, "duration_s": duration_s}

    def posture(self, name, duration_s=4.0):
        name = str(name).lower()
        if name not in POSTURES:
            raise ValueError(f"unknown posture {name!r}; try {POSTURES}")
        duration_s = max(0.2, min(15.0, float(duration_s)))
        self._post("/posture", {"name": name, "duration_s": duration_s})
        self._explicit_hold_until = time.time() + duration_s + 2.0
        return {"ok": True, "posture": name, "duration_s": duration_s}

    def actuator(self, finger, joint, side, activation, duration_s=4.0):
        """Direct per-tendon drive: side = flexor|extensor, activation 0..1."""
        self._post(
            "/actuator",
            {
                "finger": finger,
                "joint": joint,
                "side": side,
                "activation": _clamp(float(activation)),
                "duration_s": float(duration_s),
            },
        )
        return {"ok": True}

    def pose(self, spec):
        spec = lua_sandbox.to_py(spec)
        if not isinstance(spec, dict):
            raise TypeError("pose spec must be a table")
        self._post("/pose", spec)
        self._explicit_hold_until = time.time() + float(spec.get("duration_s", 2.0)) + 4.0
        return {"ok": True}

    def emotion(self, spec):
        spec = lua_sandbox.to_py(spec)
        if not isinstance(spec, dict):
            raise TypeError("emotion spec must be a table")
        payload = {}
        for k in ("stress", "arousal"):
            if k in spec:
                payload[k] = max(0.0, min(1.0, float(spec[k])))
        if "mood" in spec:
            payload["mood"] = str(spec["mood"])
        self._post("/emotion", payload)
        return {"ok": True}

    def moves(self):
        """Comma-separated move names; CSV because Python sequences don't
        cross the Lua boundary as tables."""
        return ", ".join(MOVES)

    def postures(self):
        return ", ".join(POSTURES)

    def summary(self):
        """Return a human-readable hand state string (safe for Lua display)."""
        try:
            s = self._get_json("/arm")
        except Exception as exc:  # noqa: BLE001
            return f"hand bridge offline: {exc}"
        emo = s.get("emotion", {})
        lines = [
            "hand bridge: live",
            f"goal: {s.get('goal') or 'idle'}  mood: {emo.get('mood', '?')}",
            f"stress: {int(emo.get('stress', 0) * 100)}%  arousal: {int(emo.get('arousal', 0) * 100)}%",
        ]
        for f, fd in s.get("fingers", {}).items():
            tensions = [j.get("flex_force", 0.0) for j in fd.get("joints", {}).values()]
            if tensions:
                lines.append(f"{f} tension: {int(sum(tensions) / len(tensions))}N")
        return "\n".join(lines)

    def volition(self, enabled):
        with self._lock:
            self._volition = bool(enabled)
        if enabled:
            self._ensure_started()

    def set_decide(self, fn):
        """Install (or clear, with None) the volition policy function.

        Called with a dict of inputs (mood, stress, arousal, chaos, insane,
        state); must return a move name string or None (no move this tick).
        Invoked under ``lua_lock`` because the function usually lives in the
        shared Lua runtime, which the host serializes with this lock. The
        lock is held across the policy call, so the policy must not call
        back into ``arm`` service methods (that would serialize HTTP I/O
        with all Lua dispatch).
        """
        self._decide_fn = fn

    def _choose(self, mood, stress, arousal, chaos, insane):
        if self._decide_fn is not None:
            inputs = {
                "mood": mood,
                "stress": stress,
                "arousal": arousal,
                "chaos": chaos,
                "insane": insane,
                "state": self._state,
            }
            with self._lua_lock:
                result = self._decide_fn(inputs)
            return result if isinstance(result, str) and result else None
        return self._decide(mood, stress, arousal, chaos, insane)

    def dispatch(self, args):
        """Text command dispatcher used by TUI/web slash command."""
        verb = (args[0] if args else "state").lower()
        if verb == "state" or verb == "":
            return self.summary()
        if verb == "posture":
            return f"hand: posture '{self.posture(args[1] if len(args) > 1 else 'open', float(args[2]) if len(args) > 2 else 4.0)['posture']}' set"
        if verb == "actuator":
            if len(args) < 5:
                return "usage: /hand actuator <finger> <joint> <flexor|extensor> <activation> [dur]"
            self.actuator(args[1], args[2], args[3], float(args[4]), float(args[5]) if len(args) > 5 else 4.0)
            return f"hand: {args[1]}/{args[2]} {args[3]} = {args[4]}"
        if verb == "goal":
            return f"hand: goal '{self.goal(args[1] if len(args) > 1 else 'reach', float(args[2]) if len(args) > 2 else 4.0)['goal']}' set"
        if verb == "volition":
            if len(args) > 1:
                on = args[1] in ("on", "true", "1")
            else:
                with self._lock:
                    on = not self._volition  # bare "/hand volition" toggles
            self.volition(on)
            return f"hand: volition {'enabled' if on else 'disabled'}"
        if verb == "emotion":
            spec = {}
            if len(args) > 1:
                spec["stress"] = float(args[1])
            if len(args) > 2:
                spec["arousal"] = float(args[2])
            if len(args) > 3:
                spec["mood"] = args[3]
            self.emotion(spec)
            return "hand: emotion updated"
        return "usage: /hand [state|posture <name> [dur]|actuator f j side act [dur]|goal <kind> [dur]|volition [on|off]|emotion s a mood]"

    # -- HTTP helpers ----------------------------------------------------------
    def _request(self, method, path, body=None):
        self._ensure_started()
        conn = HTTPConnection(self._host, self._port, timeout=5)
        headers = {"Content-Type": "application/json"} if body else {}
        data = json.dumps(body).encode() if body else None
        try:
            conn.request(method, path, data, headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                raise HTTPException(f"{resp.status} {raw.decode()[:200]}")
            return json.loads(raw.decode()) if raw else {}
        finally:
            conn.close()

    def _get_json(self, path):
        return self._request("GET", path)

    def _post(self, path, body):
        return self._request("POST", path, body)

    # -- SSE listener ----------------------------------------------------------
    def _sse_loop(self):
        last_id = None
        while self._alive:
            try:
                conn = HTTPConnection(self._host, self._port, timeout=10)
                headers = {}
                if last_id:
                    headers["Last-Event-ID"] = last_id
                conn.request("GET", "/events", headers=headers)
                resp = conn.getresponse()
                while self._alive:
                    line = resp.readline()
                    if not line:
                        break
                    line = line.decode().strip()
                    if line.startswith("data: "):
                        try:
                            msg = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        if msg.get("type") == "arm" and "state" in msg:
                            self._state = msg["state"]
                            self._record_telemetry(msg["state"])
                    elif line.startswith("id: "):
                        last_id = line[4:]
            except Exception as exc:  # noqa: BLE001
                log.debug("SSE loop error: %s", exc)
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
            time.sleep(2.0)

    def _record_telemetry(self, state):
        now = time.time()
        points = []
        for f, fd in state.get("fingers", {}).items():
            tension = 0.0
            strain = 0.0
            for jd in fd.get("joints", {}).values():
                tension += jd.get("flex_tension", 0)
                strain = max(strain, jd.get("strain", 0))
            n = len(fd.get("joints", {})) or 1
            points.append((now, f"{f}_tension", round(tension / n, 3)))
            points.append((now, f"{f}_strain", round(strain, 3)))
        points.append((now, "goal", state.get("goal") or "idle"))
        points.append((now, "mood", state.get("emotion", {}).get("mood", "?")))
        with self._lock:
            self._telemetry_log.extend(points)
            # keep last ~200 samples
            if len(self._telemetry_log) > 200:
                self._telemetry_log = self._telemetry_log[-200:]

    # -- volitional control -----------------------------------------------------
    def _volition_loop(self):
        while self._alive:
            time.sleep(0.5)
            with self._lock:
                if not self._volition:
                    continue
            if self.organism is None:
                continue
            try:
                s = self.organism.store
                mood = s.belief_value("self", "mood", "calm")
                stress = getattr(s, "stress", 0.0)
                arousal = getattr(s, "arousal", 0.0)
                chaos = getattr(s, "chaos", 0.0)
                insane = getattr(s, "insane", False)
            except Exception as exc:  # noqa: BLE001
                log.debug("volition read error: %s", exc)
                continue
            now = time.time()
            # avoid spamming the hand; decisions every 8-16 s depending on arousal
            interval = 16.0 - 8.0 * arousal
            with self._lock:
                if now - self._last_volition < interval:
                    continue
            # don't stomp an explicit move that is still playing out
            if now < self._explicit_hold_until:
                continue
            kind = self._volition_tick(mood, stress, arousal, chaos, insane, now)
            if kind and kind != self._last_goal:
                try:
                    self.goal(kind, duration_s=4.0 + arousal * 4.0, _volitional=True)
                    self._last_goal = kind
                    log.debug("volition chose goal %s (mood=%s stress=%.2f)", kind, mood, stress)
                except Exception as exc:  # noqa: BLE001
                    log.debug("volition goal error: %s", exc)

    def _volition_tick(self, mood, stress, arousal, chaos, insane, now):
        """Run one volition decision; never raises.

        Advances ``_last_volition`` on every evaluation (a raising policy, a
        repeated move, or no move) so re-evaluations stay on the 8-16 s
        arousal-scaled cadence instead of hot-looping on each 0.5 s tick.
        """
        self._last_volition = now
        try:
            return self._choose(mood, stress, arousal, chaos, insane)
        except Exception as exc:  # noqa: BLE001
            log.debug("volition decide error: %s", exc)
            return None

    @staticmethod
    def _decide(mood, stress, arousal, chaos, insane):
        if insane or stress > 0.78 or chaos > 0.85:
            return "fist"  # protective closure
        if stress > 0.55 or arousal > 0.75:
            return "grasp"  # reach/engage
        if mood in ("curious", "interested") or arousal > 0.5:
            return "reach"
        if mood in ("calm", "content") and stress < 0.25:
            return "wave"
        if mood in ("tired", "sleepy"):
            return "release"
        return "point"

    def stop(self):
        self._alive = False


class _DictProxy:
    """Expose Python dict keys as Lua-compatible attributes."""

    def __init__(self, data):
        self._data = data

    def __getattr__(self, name):
        try:
            val = self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        if isinstance(val, dict):
            return _DictProxy(val)
        return val

    def __contains__(self, name):
        return name in self._data

    def __iter__(self):
        return iter(self._data)

    def items(self):
        return [(k, _DictProxy(v) if isinstance(v, dict) else v) for k, v in self._data.items()]

    def __len__(self):
        return len(self._data)

    def __getitem__(self, key):
        return self._data[key]
