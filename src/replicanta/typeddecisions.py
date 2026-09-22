"""Typed-decision client: the organism's link to the local arbiter server
(a Jev-compatible typed-decision model, served by the user's own server).

Unlike the text LLM, arbiter answers *typed questions* — noul (P(statement
true)), choice (probability over named options), score (continuous index
over ordered levels) — all in one forward pass, so asking ten questions
costs about what asking one costs. Callers should always batch every
question about the same state into a single decide() call.

The contract mirrored here is the server's `/v1/systemone` endpoint:

    POST {ARBITER_URL}/v1/systemone
    {"model": "jev-latest", "state": "<text>", "questions": {"<qid>": {...}}}
    -> 200 {"model": ..., "answers": {"<qid>": {...}}, "usage": ..., "latency_ms": ...}

Configuration is env-only (the runtime replicanta.toml has no arbiter
section and none is required):

- ARBITER_URL: base URL of the server. **Unset means arbiter is not
  configured**: decide() returns None without any network I/O and every
  integration point keeps its existing behavior. Set it (e.g. to the
  default http://localhost:8010) to activate the typed-decision paths.
- ARBITER_API_KEY: sent as `Authorization: Bearer ...` only when set.
- ARBITER_TIMEOUT_MS: per-request timeout, default 2000.

decide() returns None on *any* error — unreachable server, timeout, bad
status, malformed body — after logging a warning, so callers degrade to
their existing deterministic/LLM fallbacks. After a failure the client
stays quiet for DOWNTIME_SECONDS (a down arbiter must never stall the
organism's tick behind a timeout on every utterance); one success lifts
the cooldown. Importing this module never touches the network.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://localhost:8010"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_MS = 2000
DOWNTIME_SECONDS = 30.0  # after a failure, skip attempts until this elapses

# states are prompts (debate context, chat lines); cap them so a pathological
# state can never blow up the request size
_STATE_CAP = 6000

_down_until = 0.0
_lock = threading.Lock()


# -- configuration (env, read per call like llmclient) --------------------------


def enabled():
    """Arbiter is active only when ARBITER_URL is set; unset keeps every
    caller on its current code path (no network, no behavior change)."""
    return bool(os.environ.get("ARBITER_URL"))


def arbiter_url():
    return os.environ.get("ARBITER_URL", DEFAULT_URL).rstrip("/")


def timeout_ms():
    return int(os.environ.get("ARBITER_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))


# -- question builders (wire shape per the /v1/systemone contract) ---------------


def q_noul(instructions):
    return {"type": "noul", "instructions": instructions}


def q_choice(instructions, criteria):
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def q_score(instructions, levels):
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


# -- answer accessors (defensive: a malformed answer reads as missing) -----------


def noul_of(answers, qid):
    """P(statement true) for a noul answer, or None when absent/malformed."""
    try:
        return float(answers[qid]["noul"])
    except (KeyError, TypeError, ValueError):
        return None


def choice_of(answers, qid):
    """(chosen option, its probability) for a choice answer, or None."""
    try:
        answer = answers[qid]
        name = answer["choice"]
        return name, float(answer.get("probabilities", {}).get(name, 0.0))
    except (KeyError, TypeError, ValueError):
        return None


def score_of(answers, qid):
    """The expectation (continuous level index) of a score answer, or None."""
    try:
        return float(answers[qid]["score"])
    except (KeyError, TypeError, ValueError):
        return None


# -- transport --------------------------------------------------------------------


def _mark_down():
    global _down_until
    with _lock:
        _down_until = time.monotonic() + DOWNTIME_SECONDS


def _mark_up():
    global _down_until
    with _lock:
        _down_until = 0.0


def _quiet():
    with _lock:
        return time.monotonic() < _down_until


def decide(state, questions):
    """One forward pass over every question about the state.

    `state` is free text; `questions` maps qid -> question dict (build them
    with q_noul/q_choice/q_score). Returns the server's `answers` dict
    {qid: answer}, or None on any error — callers then fall back to their
    existing path. Never raises.
    """
    if not enabled() or _quiet():
        return None
    body = {
        "model": DEFAULT_MODEL,
        "state": str(state)[:_STATE_CAP],
        "questions": questions,
    }
    data = json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    api_key = os.environ.get("ARBITER_API_KEY")
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        arbiter_url() + "/v1/systemone",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_ms() / 1000) as resp:  # nosec B310 - local arbiter endpoint
            payload = json.loads(resp.read().decode())
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise TypeError("arbiter response has no answers map")
    except (urllib.error.URLError, OSError, ValueError, TypeError) as exc:
        logger.warning("arbiter unavailable (%s); using existing fallback", exc)
        _mark_down()
        return None
    _mark_up()
    return answers
