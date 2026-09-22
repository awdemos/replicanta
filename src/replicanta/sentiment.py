"""Sentiment: scorers for how user messages touch the organism's
body. Harsh words bruise (stress up); kind words soothe (stress down).
No UI or engine imports — shared by the core (organism.hear) and the TUI.

Two layers per utterance: the keyword pass (fixed lists plus the tier B
extension vocabulary) always runs, and when the local arbiter typed-
decision server is configured (ARBITER_URL) one batched request scores
P(harsh) and P(kind) as nouls and augments the keyword result. Arbiter
answers are cached per message text so a repeated utterance never pays
twice, and any arbiter failure — including a down server, which the
client's cool-down suppresses — falls back to the keyword pass alone,
so a slow arbiter can never stall the organism's tick."""

import hashlib

from replicanta import extensions, typeddecisions

HARSHNESS_CAP = 0.15
_HARSH_HITS = 0.03
_HARSH_TERMS = (
    "stupid",
    "useless",
    "idiot",
    "pathetic",
    "worthless",
    "dumb",
    "moron",
    "loser",
    "shut up",
    "hate you",
    "ugly",
    "screw you",
    "disgusting",
    "annoying",
    "trash",
    "garbage",
    "suck",
)

KINDNESS_CAP = 0.06  # soothing is real but slower than wounding (0.15)
_KIND_HITS = 0.01
_KIND_TERMS = (
    "good",
    "love",
    "thank",
    "beautiful",
    "proud",
    "sorry",
    "please",
    "great",
    "nice",
    "sweet",
    "brave",
    "smart",
    "friend",
    "well done",
)

# per-utterance arbiter results, keyed by message-text hash; bounded FIFO so
# long sessions cannot grow it without limit
_CACHE_MAX = 256
_tone_cache: dict[str, tuple[float, float] | None] = {}


def _keyword_harshness(text):
    low = text.lower()
    terms = _HARSH_TERMS + tuple(e["text"] for e in extensions.active_entries("harsh_term"))
    hits = sum(1 for term in terms if term in low)
    return min(HARSHNESS_CAP, hits * _HARSH_HITS)


def _keyword_kindness(text):
    low = text.lower()
    terms = _KIND_TERMS + tuple(e["text"] for e in extensions.active_entries("kind_term"))
    hits = sum(1 for term in terms if term in low)
    return min(KINDNESS_CAP, hits * _KIND_HITS)


def _arbiter_tone(text):
    """(P(harsh), P(kind)) from one batched typed decision, or None when
    arbiter is unconfigured, failed, or answered nothing usable. Cached per
    message text (including None) so repeats are free."""
    if not typeddecisions.enabled():
        return None
    key = hashlib.sha256(text.encode()).hexdigest()
    if key in _tone_cache:
        return _tone_cache[key]
    answers = typeddecisions.decide(
        f"User message to the organism:\n{text}",
        {
            "harsh": typeddecisions.q_noul(
                "The user is being harsh, cruel, demeaning, or insulting to the organism in this message."
            ),
            "kind": typeddecisions.q_noul(
                "The user is being kind, warm, gentle, or appreciative to the organism in this message."
            ),
        },
    )
    tone = None
    if answers is not None:
        harsh = typeddecisions.noul_of(answers, "harsh")
        kind = typeddecisions.noul_of(answers, "kind")
        if harsh is not None or kind is not None:
            tone = (harsh or 0.0, kind or 0.0)
    if len(_tone_cache) >= _CACHE_MAX:
        _tone_cache.pop(next(iter(_tone_cache)))
    _tone_cache[key] = tone
    return tone


def harshness(text):
    """Score how harsh a user message is, 0.0 (neutral) .. HARSHNESS_CAP.
    Keyword pass (fixed list + tier B extensions), augmented by the arbiter
    P(harsh) when it is configured and answering."""
    base = _keyword_harshness(text)
    tone = _arbiter_tone(text)
    if tone is None:
        return base
    return min(HARSHNESS_CAP, max(base, tone[0] * HARSHNESS_CAP))


def kindness(text):
    """Score how kind a user message is, 0.0 (neutral) .. KINDNESS_CAP.
    Keyword pass (fixed list + tier B extensions), augmented by the arbiter
    P(kind) when it is configured and answering."""
    base = _keyword_kindness(text)
    tone = _arbiter_tone(text)
    if tone is None:
        return base
    return min(KINDNESS_CAP, max(base, tone[1] * KINDNESS_CAP))
