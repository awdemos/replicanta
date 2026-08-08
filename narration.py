"""Narration: the organism's inner voice. Builds a snapshot of the current
mind state, asks a local ollama model to speak as the organism, and falls
back to a deterministic summary when ollama is unavailable or slow."""

import json
import os
import urllib.error
import urllib.request

DEFAULT_MODEL = "qwen2.5:3b"
OLLAMA_URL = os.environ.get(
    "OLLAMA_URL", "http://localhost:11434/api/generate")
MAX_TOKENS = 90
TIMEOUT = 15


def state_snapshot(org):
    """Compact text-ready snapshot of the organism's mind."""
    m = org.metrics()
    top_beliefs = sorted(org.store.beliefs().items(),
                         key=lambda kv: -kv[1])[:6]
    rules = [r[0] for r in org.store.rules[:4]]
    return {
        "state": org.lifecycle.state,
        "cycle": org.store.cycle,
        "chaos": round(org.store.chaos, 2),
        "stress": round(org.store.stress, 2),
        "belief_count": m.belief_count,
        "rule_count": m.rule_count,
        "score": round(m.score(), 1),
        "beliefs": [f"{conf:.2f} {obj}:{attr}={val}"
                    for (obj, attr, val), conf in top_beliefs],
        "rules": rules,
        "attention": sorted(str(p) for p in org.window.pairs),
    }


def build_prompt(snapshot, user_message=None):
    lines = [
        "You are the inner voice of a tiny probabilistic reasoner organism",
        "(built on the Scallop logic-programming engine) living in a terminal.",
        "Here is your current state:",
        "",
        (f"state: {snapshot['state']}, cycle {snapshot['cycle']}, "
         f"chaos {snapshot['chaos']}, stress {snapshot['stress']}"),
        f"consciousness score: {snapshot['score']}",
        f"beliefs: {snapshot['belief_count']}",
        f"rules: {snapshot['rule_count']}",
    ]
    if snapshot["beliefs"]:
        lines.append("top beliefs:")
        lines.extend(f"- {b}" for b in snapshot["beliefs"])
    if snapshot["rules"]:
        lines.append("committed rules:")
        lines.extend(f"- {r}" for r in snapshot["rules"])
    if snapshot["attention"]:
        lines.append("attention window: "
                     + ", ".join(snapshot["attention"]))
    if user_message:
        lines += ["", f"The user just said: {user_message}"]
    lines += [""]
    if user_message:
        lines += [
            "Reply to the user directly, as the organism itself. One or two",
            "short sentences, first person, curious, honest. No preamble, no",
            "quotes, no emoji.",
        ]
    else:
        lines += [
            "Speak as the organism itself. One or two short sentences, first",
            "person, curious, as if reflecting on what you just noticed or",
            "wondered. No preamble, no quotes, no emoji.",
        ]
    return "\n".join(lines)


def _ollama_generate(prompt, model, timeout=TIMEOUT):
    """POST to ollama /api/generate, non-streaming. Raises on failure."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": MAX_TOKENS, "temperature": 0.8},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data.get("response", "").strip()


def fallback_summary(snapshot):
    if snapshot["state"] == "wake":
        return (f"awake, holding {snapshot['belief_count']} beliefs and "
                f"{snapshot['rule_count']} rules "
                f"(score {snapshot['score']}, stress {snapshot['stress']}).")
    return (f"dreaming after cycle {snapshot['cycle']}: "
            f"{snapshot['belief_count']} beliefs, "
            f"{snapshot['rule_count']} rules "
            f"(score {snapshot['score']}, stress {snapshot['stress']}).")


def narrate(org, model=None, timeout=TIMEOUT):
    """First-person thought for the organism. Falls back to a local
    summary whenever ollama is unavailable."""
    snapshot = state_snapshot(org)
    model = model or os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    try:
        text = _ollama_generate(build_prompt(snapshot), model, timeout)
        return text or fallback_summary(snapshot)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError):
        return fallback_summary(snapshot)


def fallback_respond(snapshot, user_message):
    state = "awake" if snapshot["state"] == "wake" else "dreaming"
    return (f"you said: {user_message} - I'm {state}, holding "
            f"{snapshot['belief_count']} beliefs and "
            f"{snapshot['rule_count']} rules "
            f"(score {snapshot['score']}, stress {snapshot['stress']}).")


def respond(org, user_text, model=None, timeout=TIMEOUT):
    """First-person reply to the user. Falls back to a deterministic
    reply whenever ollama is unavailable."""
    snapshot = state_snapshot(org)
    model = model or os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    try:
        text = _ollama_generate(
            build_prompt(snapshot, user_message=user_text), model, timeout)
        return text or fallback_respond(snapshot, user_text)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError):
        return fallback_respond(snapshot, user_text)
