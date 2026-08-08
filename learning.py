"""Learning: the organism picks up simple facts from what the user says —
pure pattern matching over the [a-z_]+ belief vocabulary, no LLM calls.

Belief shapes:
- "my name is X"   -> (user, name, x)          functional (replace)
- "i like/love X"  -> (user, like_x, true)     multi-valued (add)
- "i hate X"       -> (user, dislike_x, true)  multi-valued (add)
- "i am/feel X"    -> (user, feeling, x)       current state (replace)
- "you are X"      -> (self, described_as, x)  latest description (replace)
- "your X is Y"    -> (self, x, y)             functional trait (replace)
"""

import re

LEARN_CONF = 0.8
MAX_PER_MESSAGE = 2

_VALUE = r"([a-zA-Z][a-zA-Z ]{0,38}?)"

_PATTERNS = [
    (re.compile(r"\bmy name is ([a-zA-Z]+)", re.IGNORECASE),
     lambda m: ("user", "name", m.group(1)), True),
    (re.compile(r"\bi (?:really )?(?:like|love|enjoy) " + _VALUE + r"[.!,]?$", re.IGNORECASE),
     lambda m: ("user", f"like_{m.group(1)}", "true"), False),
    (re.compile(r"\bi (?:really )?(?:hate|dislike) " + _VALUE + r"[.!,]?$", re.IGNORECASE),
     lambda m: ("user", f"dislike_{m.group(1)}", "true"), False),
    (re.compile(r"\bi (?:am|feel) (?:feeling )?" + _VALUE + r"[.!,]?$", re.IGNORECASE),
     lambda m: ("user", "feeling", m.group(1)), True),
    (re.compile(r"\byour ([a-zA-Z]+) is " + _VALUE + r"[.!,]?$", re.IGNORECASE),
     lambda m: ("self", m.group(1), m.group(2)), True),
    (re.compile(r"\byou are " + _VALUE + r"[.!,]?$", re.IGNORECASE),
     lambda m: ("self", "described_as", m.group(1)), True),
]

# filler that clings to captured values and means nothing as a belief
_PREFIX_FILLER = ("really ", "very ", "so ", "quite ")
_SUFFIX_FILLER = (" too", " a lot", " very much", " so much", " a bit",
                  " actually", " honestly")


def _sanitize(text):
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
    if len(value) < 2 or len(value) > 40 or value.startswith("not"):
        return None
    if not re.match(r"^[a-z_]+$", value):
        return None
    return value


def extract(text):
    """Pull learnable facts from a user message. Returns a list of
    ((obj, attr, val), replace) — `replace` facts supersede the old reading
    for that (obj, attr); the rest accumulate. Questions teach nothing."""
    if "?" in text:
        return []
    facts = []
    for pattern, build, replace in _PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        obj, attr, val = build(match)
        val = _sanitize(val)
        if val is None:
            continue
        if attr.startswith(("like_", "dislike_")):
            prefix, raw = attr.split("_", 1)
            attr = f"{prefix}_{_sanitize(raw)}"
        else:
            attr = _sanitize(attr)
        if attr is None:
            continue
        fact = ((obj, attr, val), replace)
        if fact not in facts:
            facts.append(fact)
        if len(facts) >= MAX_PER_MESSAGE:
            break
    return facts


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
        return f"my {attr} is {val}"
    return f"{obj}:{attr}={val}"
