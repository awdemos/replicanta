"""LLM client: the shared text-generation gateway.

Supports two local backends:
- Ollama (default): `OLLAMA_URL` / `OLLAMA_MODEL`
- llama.cpp/llama-server: `LLAMACPP_URL`

Everything that talks to the local LLM lives here — the non-streaming
generate call, the vision describe call, reachability probing and
voice-health bookkeeping, token metering — plus the text helpers three
modules share: seed rotation for prompts and candidate cleaning for chatty
model output. narration.py builds prompts, arena.py debates them, mud.py
plays games with them; none of them owns the transport."""

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from replicanta import extensions

DEFAULT_MODEL = "qwen3.8-ud-hf:q3_k_m"
MAX_TOKENS = 180


def vision_model():
    """Vision model name (env: REPLICANTA_VISION_MODEL, read per call)."""
    return os.environ.get("REPLICANTA_VISION_MODEL", "moondream")


def vision_timeout():
    """Vision timeout seconds (env: REPLICANTA_VISION_TIMEOUT, per call)."""
    return int(os.environ.get("REPLICANTA_VISION_TIMEOUT", "60"))


def llm_backend():
    """Active LLM backend (env: REPLICANTA_LLM_BACKEND, default 'ollama')."""
    return os.environ.get("REPLICANTA_LLM_BACKEND", "ollama").lower()


def llama_cpp_url():
    """llama-server base URL (env: LLAMACPP_URL, default localhost:8085)."""
    return os.environ.get("LLAMACPP_URL", "http://localhost:8085")


def ollama_url():
    """Ollama generate endpoint (env: OLLAMA_URL, read per call so tests
    and runtime overrides are not frozen at import)."""
    return os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")


def default_timeout():
    """Generation timeout seconds (env: OLLAMA_TIMEOUT, read per call)."""
    return int(os.environ.get("OLLAMA_TIMEOUT", "240"))


# per-call ceiling; a 27b-class model chews a 1k-token prompt for ~90s

VOICE_PROBE_TIMEOUT = 2  # seconds for the /api/tags reachability probe
VOICE_FAILURE_STREAK = 2  # consecutive debate failures -> voice offline

# belief objects that are env metrics (background, not conversation)
ENV_OBJECTS = {"cpu", "mem", "disk", "temp", "battery", "system", "time"}


# -- voice health -----------------------------------------------------------
# Cached ollama reachability. `None` = never probed (the arena then tries the
# debate, preserving the pre-detection behavior); True/False = probed result
# or inferred from a failure streak. Only `probe_voice()` does network I/O.


class _Voice:
    def __init__(self):
        self.online = None
        self.failures = 0


_voice = _Voice()


def reset_voice():
    """Forget the cached voice state (test isolation)."""
    global _voice
    _voice = _Voice()


def _tags_url():
    parts = urllib.parse.urlsplit(ollama_url())
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/api/tags", "", ""))


def _llama_cpp_health_url():
    return f"{llama_cpp_url().rstrip('/')}/health"


def probe_voice(model=None):
    """Network probe: is the configured backend reachable? Updates the cached
    voice state and returns it."""
    if llm_backend() == "llama_cpp":
        try:
            req = urllib.request.Request(_llama_cpp_health_url())
            with urllib.request.urlopen(req, timeout=VOICE_PROBE_TIMEOUT) as resp:  # nosec B310 - local llama-server endpoint
                _voice.online = resp.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            _voice.online = False
        _voice.failures = 0
        return _voice.online

    model = model or os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    try:
        req = urllib.request.Request(_tags_url())
        with urllib.request.urlopen(req, timeout=VOICE_PROBE_TIMEOUT) as resp:  # nosec B310 - local ollama endpoint
            data = json.loads(resp.read().decode())
        names = [m.get("name", "") for m in data.get("models", [])]
        bases = [n.split(":")[0] for n in names]
        _voice.online = bool(model in names or model.split(":")[0] in bases)
    except (urllib.error.URLError, OSError, ValueError):
        _voice.online = False
    _voice.failures = 0
    return _voice.online


def voice_online():
    """Cached reachability: True/False, or None when never probed."""
    return _voice.online


def voice_status():
    """Human label for the status bar: online / offline / ? (unknown)."""
    if _voice.online is None:
        return "?"
    return "online" if _voice.online else "offline"


def note_voice_success():
    _voice.failures = 0
    _voice.online = True


def note_voice_failure():
    """A debate call failed; a streak marks the voice offline so the arena
    stops paying the timeout cost on every utterance."""
    _voice.failures += 1
    if _voice.failures >= VOICE_FAILURE_STREAK:
        _voice.online = False


# -- transport ---------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)
_SPECIAL_RE = re.compile(r"<\|[^|]*\|>")

# chat-template control tokens that reasoning models sometimes emit (and
# then loop on); stopping at them keeps generation from running away
_STOP_TOKENS = ["<|im_start|>", "<|im_end|>", "<|endoftext|>"]


def _strip_think(text):
    """Remove reasoning blocks (<think>…</think>, or an unterminated tail)
    that reasoning models (qwen3, deepseek-r1, …) sometimes emit despite
    think:false. Plain text passes through untouched."""
    return _THINK_OPEN_RE.sub("", _THINK_RE.sub("", text)).strip()


def _strip_special(text):
    """Cut everything from the first chat-template control token on
    (<|im_start|>, <|endoftext|>, …); models that miss their stop token
    otherwise loop those tokens until the token budget is gone."""
    m = _SPECIAL_RE.search(text)
    if m:
        text = text[: m.start()]
    return text.strip()


def _generate_ollama(prompt, model, timeout, temperature):
    """POST to ollama /api/generate, non-streaming."""
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "num_predict": MAX_TOKENS,
                "temperature": temperature,
                "repeat_penalty": 1.1,
                "stop": _STOP_TOKENS,
            },
        }
    ).encode()
    req = urllib.request.Request(
        ollama_url(), data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - local ollama endpoint
        data = json.loads(resp.read().decode())
    if data.get("error"):
        raise RuntimeError(data["error"])
    stats = {
        "prompt_tokens": int(data.get("prompt_eval_count") or 0),
        "gen_tokens": int(data.get("eval_count") or 0),
    }
    return _strip_special(_strip_think(data.get("response", ""))), stats


def _generate_llama_cpp(prompt, model, timeout, temperature):
    """POST to llama-server /completion, non-streaming.

    The loaded model is determined by the server, so the ``model`` argument
    is accepted for API compatibility but not sent in the payload.
    """
    payload = json.dumps(
        {
            "prompt": prompt,
            "n_predict": MAX_TOKENS,
            "temperature": temperature,
            "repeat_penalty": 1.1,
            "stop": _STOP_TOKENS,
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        f"{llama_cpp_url().rstrip('/')}/completion",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - local llama-server endpoint
        data = json.loads(resp.read().decode())
    if data.get("error"):
        raise RuntimeError(data["error"])
    stats = {
        "prompt_tokens": int(data.get("tokens_evaluated") or 0),
        "gen_tokens": int(data.get("tokens_predicted") or 0),
    }
    return _strip_special(_strip_think(data.get("content", ""))), stats


def generate_with_stats(prompt, model, timeout=None, temperature=0.95):
    """(text, stats) from the configured backend, non-streaming.

    Stats are the best prompt/gen token counts each backend exposes. Raises
    on failure.
    """
    if timeout is None:
        timeout = default_timeout()
    if llm_backend() == "llama_cpp":
        return _generate_llama_cpp(prompt, model, timeout, temperature)
    return _generate_ollama(prompt, model, timeout, temperature)


def generate(prompt, model, timeout=None, temperature=0.95):
    """Non-streaming generation through the configured backend."""
    return generate_with_stats(prompt, model, timeout, temperature)[0]


def describe_image(image_bytes, model=None, timeout=None):
    """JPEG bytes -> a short scene description from a local vision model.

    Vision is currently supported on the Ollama backend only; llama.cpp
    vision support is out of scope for this iteration.
    """
    if llm_backend() == "llama_cpp":
        raise RuntimeError("vision is not supported on the llama.cpp backend")
    if model is None:
        model = vision_model()
    if timeout is None:
        timeout = vision_timeout()
    payload = json.dumps(
        {
            "model": model,
            "prompt": (
                "Describe what is visible in this image in one or two short sentences."
            ),
            "images": [base64.b64encode(image_bytes).decode()],
            "stream": False,
            "think": False,
            "options": {"num_predict": 80, "temperature": 0.3},
        }
    ).encode()
    req = urllib.request.Request(
        ollama_url(), data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - local ollama endpoint
        data = json.loads(resp.read().decode())
    if data.get("error"):
        raise RuntimeError(data["error"])
    return _strip_think(data.get("response", ""))


# -- seeds -------------------------------------------------------------------


def seed_for(snapshot, rng, exclude=()):
    """One concrete thing for this utterance to circle around — a belief, a
    user fact, a memory, the mood, or something imagined. Rotating the seed
    every time is what keeps the voice from repeating itself; env metrics
    are deliberately excluded (they are background, not conversation).
    Seeds used recently (exclude) are avoided while alternatives remain,
    so an idle organism with a static pool still wanders."""
    pool = []
    pool += [
        f"this belief: {b}"
        for b in snapshot["beliefs"][:4]
        if b.split(" ")[1].split(":")[0] not in ENV_OBJECTS
    ]
    pool += [f"the user — {f}" for f in snapshot.get("user_facts", [])]
    pool += [f"a memory — {m}" for m in snapshot.get("memory", [])]
    if snapshot.get("user_view"):
        pool.append(f"that the user says you are {snapshot['user_view']}")
    pool.append(f"your {snapshot['mood']} mood")
    if snapshot.get("sight"):
        pool.append(f"what you see through the camera — {snapshot['sight']}")
    pool += [
        "something you wonder about the world beyond the machine",
        "a question you would love to ask the user",
        "something that might be true that you cannot verify",
    ]
    # tier B executable skills: seeds approved by the user
    pool += [e["text"] for e in extensions.active_entries("seed")]
    fresh = [p for p in pool if p not in set(exclude)]
    return rng.choice(fresh or pool)


# -- candidate cleaning -------------------------------------------------------
# chatty models love to narrate their own process ("Here is a draft of a
# candidate answer: …", "Here is the evaluation: …") instead of just
# answering; these patterns unwrap the real candidate from that preamble
# and cut any trailing self-evaluation before it leaks into an utterance
_META_PREFIX_RE = re.compile(
    r"(?is)^.*?(?:here\s+(?:is|'s)\s+(?:a|the|my)?\s*"
    r"(?:draft|candidate|answer|response|reply|possible answer)[^:\n]*:|"
    r"draft(?:\s+of\s+a\s+candidate\s+answer)?:)\s*"
)
# labels chatty models prepend to the answer itself ("Draft: …",
# "Response: …") — strip the label, keep the answer
_LABEL_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:draft|response|reply|answer|candidate)\s*:\s*"
)
_META_TAIL_RE = re.compile(
    r"(?is)\n\s*(?:here\s+is\s+the\s+(?:evaluation|critique|assessment|"
    r"revised)|evaluation:|critique:|assessment:|weakness).*$",
)
_INSTRUCTION_ECHO_RE = re.compile(
    r"(?im)^\s*(?:draft(?:ing)?\b.*|then,?\s+(?:evaluate|revise)"
    r".*|attack both candidates.*|which candidate is better\??.*)$"
)
# fragments of the utterance prompts that chatty models echo back verbatim
# (build_prompt instructions, group-chat context); a line containing any of
# these is scaffolding, not speech
_INSTRUCTION_MARKERS = (
    "no preamble",
    "no quotes",
    "no emoji",
    "worn-out words",
    "recite statistics",
    "one to three sentences",
    "as the organism itself",
    "answer your own question",
    "ask yourself one question",
    "speak from feeling",
    "reply to the user",
    "ask the user one question",
    "candidate answer",
    "attack both",
    "which candidate",
    "spun from nowhere",
    "you are in a group chat",
    "recent group conversation",
    "reply to the group",
)


def _is_repetition_loop(text, threshold=3):
    """Detect degenerate repetition: a model stuck looping the same
    sentence (or a near-twin of it) until the token budget runs out.
    Anaphora loops — sentence after sentence opening with the same words
    ("The first was …; The first was …; The first was …") — count too.
    Such output is not a candidate, it is a stuck generator."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
    if len(parts) < threshold:
        return False
    norm = [re.sub(r"\W+", " ", p.lower()).strip() for p in parts]
    top = max(norm.count(n) for n in set(norm))
    if top >= threshold:
        return True
    prefixes = [" ".join(n.split()[:3]) for n in norm if len(n.split()) >= 3]
    if len(prefixes) >= threshold:
        return max(prefixes.count(p) for p in set(prefixes)) >= threshold
    return False


def _strip_instruction_echoes(text):
    """Drop lines that are echoed prompt scaffolding rather than speech."""
    kept = [
        line
        for line in text.splitlines()
        if not _INSTRUCTION_ECHO_RE.match(line)
        and not any(m in line.lower() for m in _INSTRUCTION_MARKERS)
    ]
    return "\n".join(kept)


def clean_candidate(text):
    """Unwrap a proposer's raw output down to the answer itself: strip
    meta preambles ("Here is the draft:"), trailing self-evaluations, and
    echoed instructions. A degenerate repetition loop counts as no
    candidate at all. Returns the cleaned text (possibly empty)."""
    text = _META_PREFIX_RE.sub("", text.strip(), count=1)
    text = _META_TAIL_RE.sub("", text)
    text = _strip_instruction_echoes(text)
    text = _LABEL_PREFIX_RE.sub("", text.strip())
    text = text.strip().strip('"').strip()
    if _is_repetition_loop(text):
        return ""
    return text
