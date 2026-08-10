"""Activity meter: exact event counters for the neurosymbolic loop.

Where Metrics measures structure (what the mind holds), this measures
activity (what the mind does): every neural↔symbolic crossing is counted
at its call site and persisted with state.json.

- symbolic: derivations run by the reasoner, beliefs assimilated or
  strengthened, rules committed, dreams promoted/discarded
- neural: ollama calls and tokens (exact, from the API's own
  prompt_eval_count/eval_count), utterances manifested, fallbacks spoken
- coupling (the neurosymbolic part): facts the logic gained from the
  user's words, and the lexical-grounding proxy — utterances whose text
  reuses a content word from the seed they were drafted from

Rates are derived (totals ÷ lifecycle cycles); the counters themselves
are exact events, not estimates. What is NOT measured: 'consciousness' —
these are activity counters, deliberately not a sentience score."""

import re

SYMBOLIC_KEYS = (
    "rules_tried",
    "derivations",
    "beliefs_new",
    "beliefs_strengthened",
    "beliefs_archived",
    "rules_committed",
    "dreams_promoted",
    "dreams_discarded",
)
NEURAL_KEYS = ("llm_calls", "prompt_tokens", "gen_tokens", "utterances", "fallbacks")
COUPLING_KEYS = ("facts_learned", "grounded_utterances")

# scaffolding words that appear in every seed — not evidence of grounding
_SEED_SCAFFOLD = {
    "this",
    "belief",
    "that",
    "your",
    "mood",
    "user",
    "says",
    "memory",
    "cycle",
    "what",
    "most",
    "alive",
    "right",
    "now",
    "question",
    "would",
    "love",
    "something",
    "wonder",
    "about",
    "world",
    "beyond",
    "machine",
    "cannot",
    "verify",
    "might",
    "true",
    "them",
    "their",
    "life",
    "the",
    "and",
    "for",
    "you",
    "are",
    "its",
    "all",
    "any",
    "out",
    "who",
    "why",
    "how",
    "did",
    "can",
    "but",
    "not",
    "yet",
    "too",
}

_WORD_RE = re.compile(r"[a-z_]{3,}")


def note(store, key, n=1):
    """Increment one counter on the organism's belief store."""
    store.note_activity(key, n)


def grounded(seed, text):
    """Lexical-grounding proxy: True when the utterance reuses at least
    one content word from the seed it was drafted from. A cheap signal
    that the voice was shaped by the logic it was shown, not a proof."""
    seed_words = {w for w in _WORD_RE.findall(seed.lower())} - _SEED_SCAFFOLD
    if not seed_words:
        return False
    text_words = set(_WORD_RE.findall(text.lower()))
    return bool(seed_words & text_words)


def _rate(count, cycle):
    return count / max(cycle, 1)


def summary_lines(store):
    """Human-readable totals + per-cycle rates, for /stats and the Mind
    tab. Returns [] when nothing has happened yet."""
    a = store.activity
    if not a:
        return []
    cycle = store.cycle

    def rate(key):
        return f"{_rate(a.get(key, 0), cycle):.2f}/cycle"

    lines = ["activity (total · per cycle)", ""]
    lines.append(
        f"symbolic: {a.get('derivations', 0)} derivations over "
        f"{a.get('rules_tried', 0)} questions ({rate('derivations')}) · "
        f"{a.get('beliefs_new', 0)} new + {a.get('beliefs_strengthened', 0)} "
        f"strengthened beliefs · {a.get('rules_committed', 0)} rules "
        f"committed · dreams {a.get('dreams_promoted', 0)} promoted / "
        f"{a.get('dreams_discarded', 0)} discarded"
    )
    lines.append(
        f"neural: {a.get('llm_calls', 0)} llm calls · "
        f"{a.get('prompt_tokens', 0)} tokens in / "
        f"{a.get('gen_tokens', 0)} out · {a.get('utterances', 0)} "
        f"utterances manifested ({a.get('fallbacks', 0)} fallbacks)"
    )
    utterances = a.get("utterances", 0)
    grounded_share = (
        f"{a.get('grounded_utterances', 0)}/{utterances}" if utterances else "—"
    )
    lines.append(
        f"coupling: {a.get('facts_learned', 0)} facts learned from the "
        f"user ({rate('facts_learned')}) · grounded utterances "
        f"{grounded_share}"
    )
    return lines


def record_digest(store, cycles=30):
    """Record an activity snapshot and return a short narrative of recent
    learning activity, for the voice prompt. Mutates store.activity
    (appends/prunes snapshots) — the name says so.

    Compares current counters against a snapshot taken roughly `cycles`
    cycles ago. If no history exists yet, reports lifetime totals.
    """
    a = store.activity
    if not a:
        return "you have not done much yet"

    snapshots = a.setdefault("snapshots", [])
    now = store.cycle

    # Record a snapshot for the current cycle if we don't have one yet.
    if not snapshots or snapshots[-1]["cycle"] != now:
        snapshot = {
            "cycle": now,
            "counters": {
                k: a.get(k, 0) for k in (SYMBOLIC_KEYS + NEURAL_KEYS + COUPLING_KEYS)
            },
        }
        snapshots.append(snapshot)

    # Keep only snapshots within the window plus one anchor.
    cutoff = now - cycles
    while len(snapshots) > 1 and snapshots[1]["cycle"] <= cutoff:
        snapshots.pop(0)

    anchor = snapshots[0]
    elapsed = max(now - anchor["cycle"], 1)
    before = anchor["counters"]

    def delta(key):
        return a.get(key, 0) - before.get(key, 0)

    tried = delta("rules_tried")
    derived = delta("derivations")
    committed = delta("rules_committed")
    promoted = delta("dreams_promoted")
    discarded = delta("dreams_discarded")
    beliefs_new = delta("beliefs_new")
    llm_calls = delta("llm_calls")
    fallbacks = delta("fallbacks")

    rate = derived / max(tried, 1)
    dream_rate = promoted / max(promoted + discarded, 1)

    lines = [f"over the last {elapsed} cycles you have:"]
    lines.append(
        f"- asked {tried} self-questions and produced {derived} derivations "
        f"({rate:.0%} yield)"
    )
    lines.append(
        f"- committed {committed} rules, promoted {promoted} dreams, and "
        f"discarded {discarded} dreams ({dream_rate:.0%} dream promotion)"
    )
    lines.append(
        f"- formed {beliefs_new} new beliefs and used your inner voice "
        f"{llm_calls} times ({fallbacks} fallbacks)"
    )
    return "\n".join(lines)
