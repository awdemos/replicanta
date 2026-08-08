"""Sentiment: pure text scorers for how user messages touch the organism's
body. Harsh words bruise (stress up); kind words soothe (stress down).
No UI or engine imports — shared by the core (organism.hear) and the TUI."""

HARSHNESS_CAP = 0.15
_HARSH_HITS = 0.03
_HARSH_TERMS = (
    "stupid", "useless", "idiot", "pathetic", "worthless", "dumb",
    "moron", "loser", "shut up", "hate you", "ugly", "screw you",
    "disgusting", "annoying", "trash", "garbage", "suck",
)

KINDNESS_CAP = 0.02
_KIND_HITS = 0.01
_KIND_TERMS = (
    "good", "love", "thank", "beautiful", "proud", "sorry", "please",
    "great", "nice", "sweet", "brave", "smart", "friend", "well done",
)


def harshness(text):
    """Score how harsh a user message is, 0.0 (neutral) .. HARSHNESS_CAP."""
    low = text.lower()
    hits = sum(1 for term in _HARSH_TERMS if term in low)
    return min(HARSHNESS_CAP, hits * _HARSH_HITS)


def kindness(text):
    """Score how kind a user message is, 0.0 (neutral) .. KINDNESS_CAP."""
    low = text.lower()
    hits = sum(1 for term in _KIND_TERMS if term in low)
    return min(KINDNESS_CAP, hits * _KIND_HITS)
