"""Hybrid memory ranking: importance at write time + relevance at read time.

Memories are small episodic records. We want the organism's inner voice to
recall events that matter, not just the last few things that happened.
"""

import re
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[a-z0-9_']+")

KIND_WEIGHTS = {
    "faded": 0.4,
    "revived": 0.4,
    "born": 0.35,
    "harsh": 0.35,
    "surprise": 0.3,
    "kind": 0.25,
    "goal": 0.2,
    "command": 0.15,
    "learned": 0.15,
    "diary": 0.1,
    "dream": 0.05,
    "mud": 0.05,
}


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) > 1]


def score_importance(
    kind: str,
    text: str,
    cycle: int,
    current_cycle: int = 0,
) -> float:
    """Heuristic importance in 0..1.

    Weighs emotional/lifecycle kinds heavily, boosts user-facing and
    inquisitive text, and gives a small recency bonus.
    """
    score = 0.5 + KIND_WEIGHTS.get(kind, 0.0)
    lower = text.lower()
    if "user" in lower:
        score += 0.1
    if "?" in text:
        score += 0.05
    if kind in ("harsh", "kind"):
        score += 0.05
    # tiny recency bump, capped so old but important events stay selectable
    age = max(0, current_cycle - cycle)
    score += 0.02 * min(10, max(0, 10 - age / max(1, current_cycle / 10 + 1)))
    return min(1.0, score)


def score_relevance(memory_text: str, query: str) -> float:
    """Token-overlap relevance in 0..1."""
    mem_tokens = set(_tokens(memory_text))
    query_tokens = set(_tokens(query))
    if not mem_tokens or not query_tokens:
        return 0.0
    overlap = len(mem_tokens & query_tokens)
    return overlap / max(len(mem_tokens), len(query_tokens))


@dataclass
class MemoryScorer:
    """Stateful scorer that tracks recall counts and ranks memories."""

    importance_weight: float = 0.6
    relevance_weight: float = 0.3
    recall_weight: float = 0.1

    def rank(
        self,
        memories: list[dict],
        query: str,
        top_k: int = 8,
        current_cycle: int = 0,
    ) -> list[dict]:
        """Return the top-k memories by combined score.

        Memories are returned in ranked order. The original dicts are not
        copied; callers may increment ``recall`` on the returned items.
        """

        def _key(memory: dict) -> float:
            importance = memory.get("importance", 0.5)
            if "importance" not in memory and "kind" in memory:
                importance = score_importance(
                    memory.get("kind", "learned"),
                    memory.get("text", ""),
                    memory.get("cycle", 0),
                    current_cycle=current_cycle,
                )
            relevance = score_relevance(memory.get("text", ""), query)
            recall = min(1.0, memory.get("recall", 0) / 100)
            return (
                self.importance_weight * importance
                + self.relevance_weight * relevance
                + self.recall_weight * recall
            )

        ranked = sorted(memories, key=_key, reverse=True)
        return ranked[:top_k]

    @staticmethod
    def mark_recalled(memory: dict):
        """Increment the recall counter on a memory dict."""
        memory["recall"] = memory.get("recall", 0) + 1


def attach_importance(
    memory: dict,
    current_cycle: int = 0,
) -> dict:
    """Ensure a memory dict carries an ``importance`` score."""
    if "importance" not in memory:
        memory["importance"] = score_importance(
            memory.get("kind", "learned"),
            memory.get("text", ""),
            memory.get("cycle", 0),
            current_cycle=current_cycle,
        )
    if "recall" not in memory:
        memory["recall"] = 0
    return memory
