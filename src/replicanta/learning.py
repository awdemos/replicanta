"""Learning: extract beliefs, goals, commands, and questions from user chat.

The pipeline has three tiers:
1. Fast regex patterns over a small belief vocabulary (no network).
2. Synonym normalization and speech-act classification (statement / question /
   command / intent / negation).
3. Optional LLM fallback for complex statements that regex misses; those facts
   carry lower confidence and can be surfaced for confirmation instead of
   applying immediately.

Belief shapes:
- "my name is X"      -> (user, name, x)          functional (replace)
- "my X is Y"         -> (user, x, y)             functional (replace)
- "i like/love X"     -> (user, like_x, true)     multi-valued (add)
- "i hate X"          -> (user, dislike_x, true)  multi-valued (add)
- "i am/feel X"       -> (user, feeling, x)       current state (replace)
- "you are X"         -> (self, described_as, x)  latest description (replace)
- "your X is Y"       -> (self, x, y)             functional trait (replace)
- "X is Y" / "X means Y" -> (self, knows, x_is_y) definitional (replace)

Intent / goal shapes:
- "i want to X" / "i want X" / "i need X" / "i would like X" / "let's X" /
  "i hope to X" / "remind me to X" / "learn about X" -> goal text

Command shapes:
- "please X", "can you X", "set X to Y", "make X Y", "do X" -> command text
"""

import json
import os
import re

from replicanta import extensions

LEARN_CONF = 0.8
LLM_CONF = 0.5
MAX_PER_MESSAGE = 2

_VALUE = r"([a-zA-Z][a-zA-Z ]{0,58}?)"
_WORD = r"([a-zA-Z][a-zA-Z ]{0,38}?)"

# -- synonym normalization ----------------------------------------------------
# Maps surface words to a canonical belief vocabulary term.
GENERIC_SYNONYMS = {
    "yep": "yes",
    "yeah": "yes",
    "nope": "no",
    "nah": "no",
}

FEELING_SYNONYMS = {
    "glad": "happy",
    "cheerful": "happy",
    "joyful": "happy",
    "unhappy": "sad",
    "melancholy": "sad",
    "depressed": "sad",
    "down": "sad",
    "blue": "sad",
    "mad": "angry",
    "furious": "angry",
    "sleepy": "tired",
    "exhausted": "tired",
    "wired": "excited",
    "thrilled": "excited",
    "afraid": "scared",
    "terrified": "scared",
    "anxious": "scared",
}


# -- regex patterns -----------------------------------------------------------

_PATTERNS = [
    (
        re.compile(r"\bmy name is ([a-zA-Z]+)", re.IGNORECASE),
        lambda m: ("user", "name", m.group(1)),
        True,
    ),
    (
        re.compile(r"\bmy " + _WORD + r" is " + _VALUE + r"[.!,]?$", re.IGNORECASE),
        lambda m: ("user", m.group(1), m.group(2)),
        True,
    ),
    (
        re.compile(
            r"\bi (?:really )?(?:like|love|enjoy) " + _VALUE + r"[.!,]?$", re.IGNORECASE
        ),
        lambda m: ("user", f"like_{m.group(1)}", "true"),
        False,
    ),
    (
        re.compile(
            r"\bi (?:really )?(?:hate|dislike) " + _VALUE + r"[.!,]?$", re.IGNORECASE
        ),
        lambda m: ("user", f"dislike_{m.group(1)}", "true"),
        False,
    ),
    (
        re.compile(
            r"\bi (?:am|feel) (?:feeling )?" + _VALUE + r"[.!,]?$", re.IGNORECASE
        ),
        lambda m: ("user", "feeling", m.group(1)),
        True,
    ),
    (
        re.compile(r"\byour ([a-zA-Z]+) is " + _VALUE + r"[.!,]?$", re.IGNORECASE),
        lambda m: ("self", m.group(1), m.group(2)),
        True,
    ),
    (
        re.compile(r"\byou are " + _VALUE + r"[.!,]?$", re.IGNORECASE),
        lambda m: ("self", "described_as", m.group(1)),
        True,
    ),
    (
        re.compile(
            r"^([a-zA-Z]+) (?:is|means) " + _VALUE + r"[.!,]?$", re.IGNORECASE
        ),
        lambda m: ("self", "knows", f"{m.group(1)}_is_{m.group(2)}"),
        True,
    ),
]

_NEGATION_PATTERNS = [
    (
        re.compile(
            r"\bi (?:do not|don't) (?:like|love|enjoy) " + _VALUE + r"[.!,]?$",
            re.IGNORECASE,
        ),
        lambda m: ("user", f"dislike_{m.group(1)}", "true"),
        False,
    ),
    (
        re.compile(
            r"\bi (?:am|feel) (?:feeling )?not " + _VALUE + r"[.!,]?$", re.IGNORECASE
        ),
        lambda m: ("user", "feeling", f"not_{m.group(1)}"),
        True,
    ),
    (
        re.compile(r"\byou are not " + _VALUE + r"[.!,]?$", re.IGNORECASE),
        lambda m: ("self", "described_as", f"not_{m.group(1)}"),
        True,
    ),
]

_INTENT_PATTERNS = [
    re.compile(r"\bi want to (.+?)[.!,]?$", re.IGNORECASE),
    re.compile(r"\bi want (.+?)[.!,]?$", re.IGNORECASE),
    re.compile(r"\bi need (.+?)[.!,]?$", re.IGNORECASE),
    re.compile(r"\bi would like (.+?)[.!,]?$", re.IGNORECASE),
    re.compile(r"\blet'?s (.+?)[.!,]?$", re.IGNORECASE),
    re.compile(r"\bi hope to (.+?)[.!,]?$", re.IGNORECASE),
    re.compile(r"\bremind me to (.+?)[.!,]?$", re.IGNORECASE),
    re.compile(r"\blearn about (.+?)[.!,]?$", re.IGNORECASE),
]

_COMMAND_PATTERNS = [
    re.compile(r"\b(?:can you|could you) (.+?)[.!?]?$", re.IGNORECASE),
    re.compile(r"\bplease (.+?)[.!?]?$", re.IGNORECASE),
    re.compile(r"\bset " + _WORD + r" to " + _VALUE + r"[.!?]?$", re.IGNORECASE),
    re.compile(r"\bmake " + _WORD + r" " + _VALUE + r"[.!?]?$", re.IGNORECASE),
]

# filler that clings to captured values and means nothing as a belief
_PREFIX_FILLER = ("really ", "very ", "so ", "quite ")
_SUFFIX_FILLER = (
    " too",
    " a lot",
    " very much",
    " so much",
    " a bit",
    " actually",
    " honestly",
)


def _normalize_word(value, attr=None):
    """Map a single word to its canonical synonym if one exists.

    Feeling synonyms are only applied to feeling/described_as values so that
    unrelated words like "blue" keep their literal meaning.
    """
    canonical = GENERIC_SYNONYMS.get(value, value)
    if attr in ("feeling", "described_as"):
        canonical = FEELING_SYNONYMS.get(canonical, canonical)
    return canonical


def _sanitize(text, attr=None):
    """Free text -> [a-z_]+ belief vocabulary, or None when nothing
    meaningful survives (punctuation, numbers, filler-only)."""
    value = text.lower().strip()
    value = re.sub(r"[^a-z_ ]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    for filler in _PREFIX_FILLER:
        value = value.removeprefix(filler)
    for filler in _SUFFIX_FILLER:
        value = value.removesuffix(filler)
    value = value.strip().replace(" ", "_")
    # apply synonym normalization word-by-word
    if "_" in value:
        value = "_".join(_normalize_word(w, attr) for w in value.split("_"))
    else:
        value = _normalize_word(value, attr)
    if len(value) < 2 or len(value) > 40:
        return None
    if not re.match(r"^[a-z_]+$", value):
        return None
    return value


def _sanitize_attr(text):
    """Attributes are shorter and never start with a digit."""
    value = _sanitize(text)
    if value is None:
        return None
    value = value[:24]
    if value.startswith(("like_", "dislike_")):
        prefix, raw = value.split("_", 1)
        raw = _sanitize(raw)
        if raw is None:
            return None
        return f"{prefix}_{raw}"
    return value


def _classify_speech_act(text):
    """Rough speech-act tag used to route extraction."""
    lower = text.strip().lower()
    if text.endswith("?"):
        return "question"
    if lower.startswith(
        ("i want ", "i need ", "i would like ", "let's ", "i hope to ")
    ) or re.search(r"\bremind me to\b", lower):
        return "intent"
    if lower.startswith(("please ", "can you ", "could you ", "set ", "make ")):
        return "command"
    if re.search(r"\bnot\b|n't|\bdont\b", lower):
        return "negation"
    return "statement"


def _extract_facts(text, speech_act):
    """Return a list of (belief, replace) tuples from regex patterns."""
    facts = []
    patterns = list(_PATTERNS)
    if speech_act == "negation":
        patterns = _NEGATION_PATTERNS + patterns
    for pattern, build, replace in patterns:
        match = pattern.search(text)
        if match is None:
            continue
        obj, attr, val = build(match)
        attr = _sanitize_attr(attr)
        if attr is None:
            continue
        val = _sanitize(val, attr)
        if val is None:
            continue
        if attr is None:
            continue
        fact = ((obj, attr, val), replace)
        if fact not in facts:
            facts.append(fact)
        if len(facts) >= MAX_PER_MESSAGE:
            break
    # tier B executable skills: registry patterns approved by the user
    for entry in extensions.active_entries("pattern"):
        match = re.search(entry["regex"], text, re.IGNORECASE)
        if match is None:
            continue
        raw = match.group(1) if match.groups() else match.group(0)
        value = _sanitize(raw)
        if value is None:
            continue
        obj, attr, val = (
            part.replace("{x}", value) for part in entry["template"].split(":")
        )
        fact = ((obj, attr, val), obj == "self")
        if fact not in facts:
            facts.append(fact)
        if len(facts) >= MAX_PER_MESSAGE:
            break
    return facts


def _extract_goal(text):
    """Return a goal string if the text is intent-bearing, else None."""
    for pattern in _INTENT_PATTERNS:
        match = pattern.search(text)
        if match:
            goal = match.group(1).strip().rstrip(".!,")
            if pattern.pattern.startswith(r"\blearn about "):
                return f"learn about {goal}"
            if pattern.pattern.startswith(r"\bremind me to "):
                return f"remind: {goal}"
            return goal
    return None


def _extract_command(text):
    """Return a command string if the text is imperative, else None."""
    for pattern in _COMMAND_PATTERNS:
        match = pattern.search(text)
        if match:
            if len(match.groups()) == 2:
                return f"set {match.group(1).strip()} to {match.group(2).strip()}"
            return match.group(1).strip().rstrip(".!,")
    return None


def _llm_enabled():
    """The regex-only path is the default; set REPLICANTA_LEARNING_LLM=1 to
    enable the LLM fallback for complex statements."""
    return os.environ.get("REPLICANTA_LEARNING_LLM") == "1"


def _llm_extract(text):
    """Ask the local LLM to extract structured facts from a complex sentence.
    Returns a list of {"subject": "user|self", "relation": ..., "object": ...}
    dicts. Network failures are swallowed."""
    try:
        from replicanta import llmclient

        prompt = (
            "Extract simple beliefs from the sentence. Output ONLY valid JSON in this "
            "exact shape with no preamble:\n"
            '{"facts":[{"subject":"user","relation":"name","object":"sam"}]}\n'
            f"Sentence: {text}\nJSON:"
        )
        raw = llmclient.generate(prompt, llmclient.DEFAULT_MODEL, temperature=0.2)
        # grab the first JSON object, in case the model adds chatter
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return []
        data = json.loads(raw[start : end + 1])
        facts = []
        for item in data.get("facts", []):
            sub = str(item.get("subject", "")).lower()
            rel = str(item.get("relation", ""))
            obj = str(item.get("object", ""))
            if sub not in ("user", "self"):
                continue
            rel = _sanitize_attr(rel)
            obj = _sanitize(obj)
            if rel is None or obj is None:
                continue
            facts.append({"subject": sub, "relation": rel, "object": obj})
        return facts
    except Exception:  # noqa: BLE001 — LLM fallback must never break learning
        return []


def analyze(text, context=None, use_llm=None):
    """Full analysis of a user message.

    Returns a dict:
        {
            "speech_act": "statement" | "question" | "command" | "intent" | "negation" | "unknown",
            "facts": [ {"belief": (obj, attr, val), "replace": bool, "confidence": float}, ... ],
            "goals": [ str, ... ],
            "commands": [ str, ... ],
            "question": bool,
        }

    `context` is an optional topic string used to expand bare pronouns.
    `use_llm` overrides the REPLICANTA_LEARNING_LLM env flag.
    """
    text = str(text).strip()
    result = {
        "speech_act": "unknown",
        "facts": [],
        "goals": [],
        "commands": [],
        "question": False,
    }
    if not text:
        return result

    if context:
        # crude pronoun grounding: "it is X" -> "<context> is X"
        text = re.sub(r"\bit\b", context, text, flags=re.IGNORECASE)

    speech_act = _classify_speech_act(text)
    result["speech_act"] = speech_act

    if speech_act == "question":
        result["question"] = True

    command = _extract_command(text)
    if command:
        result["commands"].append(command)

    goal = _extract_goal(text)
    if goal:
        result["goals"].append(goal)

    for belief, replace in _extract_facts(text, speech_act):
        result["facts"].append(
            {"belief": belief, "replace": replace, "confidence": LEARN_CONF}
        )

    if (
        (use_llm if use_llm is not None else _llm_enabled())
        and not result["facts"]
        and speech_act in ("statement", "unknown")
    ):
        for item in _llm_extract(text):
            belief = (item["subject"], item["relation"], item["object"])
            result["facts"].append(
                {"belief": belief, "replace": True, "confidence": LLM_CONF}
            )

    return result


def extract(text):
    """Pull learnable facts from a user message. Returns a list of
    ((obj, attr, val), replace) — kept for backward compatibility with callers
    that only need high-confidence regex facts.

    Questions teach nothing, but intent/command utterances are ignored here;
    use `analyze()` to capture those.
    """
    result = analyze(text)
    return [
        (item["belief"], item["replace"])
        for item in result["facts"]
        if item["confidence"] >= LEARN_CONF
    ]


def describe(belief):
    """One human sentence for a learned belief (log lines + prompt)."""
    obj, attr, val = belief
    if obj == "user":
        if attr == "name":
            return f"your name is {val}"
        if attr.startswith("like_"):
            return f"you like {attr[5:].replace('_', ' ')}"
        if attr.startswith("dislike_"):
            return f"you dislike {attr[8:].replace('_', ' ')}"
        if attr == "feeling":
            return f"you feel {val}"
    if obj == "self":
        if attr == "described_as":
            return f"you say I am {val}"
        if attr == "knows":
            return f"you told me {val.replace('_', ' ')}"
        return f"my {attr} is {val}"
    return f"{obj}:{attr}={val}"
