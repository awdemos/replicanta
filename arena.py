"""Thought arena: the organism's inner debate. Every utterance the
organism manifests — idle musings, replies, questions to the user,
self-talk, goals, diary entries, reflections — runs through a small
adversarial chamber instead of a single solo model call: two proposers
independently draft a candidate, an adversarial critic attacks both, and
two voters pick a majority winner (or, when the vote deadlocks, the
critic's own preference tips the tie, and a random draw decides a truly
indifferent deadlock). The per-round temperature jitters so the debate
is never two identical passes, and in high chaos the organism may inject
a rogue thought of its own (never for structured tasks like reflections,
whose output contract a rogue candidate would break). Any ollama failure
at any stage falls back to the local deterministic answers, so the
organism always has a voice."""

import os
import random
import re
import urllib.error

import activity
import narration

VOTE_PREFIX = "VOTE: "
VOTE_RE = re.compile(r"VOTE:\s*([12])")

# chaos -> probability the second proposal is replaced by a rogue
# thought of the organism's own devising. The highest chaos level at or
# below the current (stress-nudged) chaos applies; below 0.3 a small
# default keeps the world from being entirely predictable.
CHAOS_SURPRISE_ODDS = {0.3: 0.05, 0.5: 0.10, 0.7: 0.25, 1.0: 0.50}
CHAOS_SURPRISE_DEFAULT = 0.02

# The rogue thought is a prompt fragment: when it fires, the second
# proposer is replaced by this instruction so the model actually
# generates the rogue thought instead of the draft being a literal.
ROGUE_THOUGHT = ("Draft a rogue thought of your own, spun from nowhere - "
                 "it may contradict your beliefs, your rules, even "
                 "yourself. Keep it to one to three sentences. No "
                 "preamble, no quotes, no emoji.")

TEMP_MIN = 0.7      # lower bound for the per-round temperature jitter
TEMP_MAX = 0.85     # upper bound

# chatty models love to narrate their own process ("Here is a draft of a
# candidate answer: …", "Here is the evaluation: …") instead of just
# answering; these patterns unwrap the real candidate from that preamble
# and cut any trailing self-evaluation before it leaks into an utterance
_META_PREFIX_RE = re.compile(
    r"(?is)^.*?(?:here\s+(?:is|'s)\s+(?:a|the|my)?\s*"
    r"(?:draft|candidate|answer|response|reply|possible answer)[^:\n]*:|"
    r"draft(?:\s+of\s+a\s+candidate\s+answer)?:)\s*")
# labels chatty models prepend to the answer itself ("Draft: …",
# "Response: …") — strip the label, keep the answer
_LABEL_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:draft|response|reply|answer|candidate)\s*:\s*")
_META_TAIL_RE = re.compile(
    r"(?is)\n\s*(?:here\s+is\s+the\s+(?:evaluation|critique|assessment|"
    r"revised)|evaluation:|critique:|assessment:|weakness).*$",)
_INSTRUCTION_ECHO_RE = re.compile(
    r"(?im)^\s*(?:draft(?:ing)?\b.*|then,?\s+(?:evaluate|revise)"
    r".*|attack both candidates.*|which candidate is better\??.*)$")
# fragments of the utterance prompts that chatty models echo back verbatim
# (build_prompt instructions, group-chat context); a line containing any of
# these is scaffolding, not speech
_INSTRUCTION_MARKERS = (
    "no preamble", "no quotes", "no emoji", "worn-out words",
    "recite statistics", "one to three sentences",
    "as the organism itself", "answer your own question",
    "ask yourself one question", "speak from feeling",
    "reply to the user", "ask the user one question",
    "candidate answer", "attack both", "which candidate",
    "spun from nowhere",
    "you are in a group chat", "recent group conversation",
    "reply to the group",
)


def _is_repetition_loop(text, threshold=3):
    """Detect degenerate repetition: a model stuck looping the same
    sentence (or a near-twin of it) until the token budget runs out.
    Anaphora loops — sentence after sentence opening with the same words
    ("The first was …; The first was …; The first was …") — count too.
    Such output is not a candidate, it is a stuck generator."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text)
             if p.strip()]
    if len(parts) < threshold:
        return False
    norm = [re.sub(r"\W+", " ", p.lower()).strip() for p in parts]
    top = max(norm.count(n) for n in set(norm))
    if top >= threshold:
        return True
    prefixes = [" ".join(n.split()[:3]) for n in norm
                if len(n.split()) >= 3]
    if len(prefixes) >= threshold:
        return max(prefixes.count(p) for p in set(prefixes)) >= threshold
    return False


def _strip_instruction_echoes(text):
    """Drop lines that are echoed prompt scaffolding rather than speech."""
    kept = [line for line in text.splitlines()
            if not _INSTRUCTION_ECHO_RE.match(line)
            and not any(m in line.lower() for m in _INSTRUCTION_MARKERS)]
    return "\n".join(kept)


def _clean_candidate(text):
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


class ThoughtArena:
    """One debate per utterance. Stateless apart from a per-call RNG, so
    a single instance is safe to share."""

    def __init__(self, rng=None, model=None, timeout=None):
        self._rng = rng if rng is not None else random.Random()
        self._model = model
        self._timeout = timeout

    # -- public ----------------------------------------------------------
    def emerge(self, org, user_message=None, prompt_kwargs=None,
               fallback=None, structured=False, on_token=None,
               model=None, timeout=None, quick=False):
        """Run a full debate and return the winning candidate.

        prompt_kwargs are forwarded to narration.build_prompt to select
        the task (ask_user, self_ask, self_question, form_goal, diary,
        reflect); user_message selects the reply task. fallback is a
        callable taking the state snapshot, used when the voice is
        offline or the debate fails (default: the local summary/reply).
        structured=True suppresses the rogue-thought injection so the
        output contract (e.g. the reflection format) survives. The
        debate itself cannot stream, so a winner is replayed through
        on_token in word chunks to keep the incremental display alive.
        quick=True replaces the five-call debate with a single cleaned
        generation — for many-speaker contexts (group chat) where a
        full debate per utterance would cost minutes.
        """
        model = (model or self._model
                 or os.environ.get("OLLAMA_MODEL", narration.DEFAULT_MODEL))
        timeout = timeout or self._timeout or narration.TIMEOUT
        snapshot = narration.state_snapshot(org)
        # every debate circles a different concrete thing — this rotation is
        # what keeps the idle voice from repeating itself; the last few
        # seeds are excluded so a static pool (idle organism) still varies.
        # The history lives on the organism object: per-organism, and it
        # resets naturally on swap or restart.
        recent_seeds = getattr(org, "_recent_seeds", None)
        if recent_seeds is None:
            from collections import deque
            recent_seeds = org._recent_seeds = deque(maxlen=6)
        snapshot["seed"] = narration._seed_for(snapshot, self._rng,
                                               exclude=recent_seeds)
        recent_seeds.append(snapshot["seed"])
        # the whole organism treats chaos as stress-nudged
        # (organism.chaos_effective()); the arena should too, so surprise
        # rises as the organism gets stressed, not just on the raw knob
        effective = getattr(org, "chaos_effective", lambda: snapshot["chaos"])()
        surprise = 0.0 if structured else self._surprise_for(effective)
        # temperature=0 in the snapshot means deterministic probe mode
        # (tests); anything else jitters per round
        temperature = 0.0 if snapshot.get("temperature") == 0 else None
        # voice known-offline: skip the debate entirely so replies stay
        # instant instead of paying an ollama timeout on every utterance
        if narration.voice_online() is False:
            return self._fallback(org.store, snapshot, user_message, fallback)
        build = {"user_message": user_message}
        build.update(prompt_kwargs or {})
        try:
            if quick:
                result = self._quick_take(org, snapshot, build, model,
                                          timeout, temperature)
            else:
                result = self._debate(org, snapshot, build, model,
                                      timeout, surprise, temperature)
        except (urllib.error.URLError, OSError, ValueError, RuntimeError):
            narration.note_voice_failure()
            return self._fallback(org.store, snapshot, user_message, fallback)
        narration.note_voice_success()
        activity.note(org.store, "utterances")
        grounded = activity.grounded(snapshot["seed"], result)
        if grounded:
            activity.note(org.store, "grounded_utterances")
        skill_store = getattr(org, "skills", None)
        if skill_store is not None:
            outcome = {
                "grounded": grounded,
                "user_replied": bool(user_message),
                "new_belief": False,
            }
            for skill in snapshot.get("relevant_skills", []):
                skill_store.record_use(
                    skill.name, cycle=org.store.cycle, outcome=outcome)
        if on_token is not None:
            for piece in re.findall(r"\S+\s*", result):
                on_token(piece)
        return result

    # -- debate ----------------------------------------------------------
    def _quick_take(self, org, snapshot, build, model, timeout,
                    temperature):
        """One proposer, no debate: a single generation cleaned down to
        the candidate. Empty or degenerate output fails the take so the
        caller falls back, exactly like a failed debate."""
        base = narration.build_prompt(snapshot, **build)
        draft = self._generate(self._proposal(base, 1), model, timeout,
                               temperature, org=org)
        draft = _clean_candidate(draft)
        if not draft:
            raise ValueError("quick take produced no usable candidate")
        return draft

    def _debate(self, org, snapshot, build, model, timeout,
                surprise, temperature):
        base = narration.build_prompt(snapshot, **build)
        drafts = [
            self._generate(self._proposal(base, 1), model, timeout,
                           temperature, org=org),
        ]
        if self._rng.random() < surprise:
            drafts.append(self._generate(self._rogue_proposal(base), model,
                                         timeout, temperature, org=org))
        else:
            drafts.append(self._generate(self._proposal(base, 2), model,
                                         timeout, temperature, org=org))
        # a proposer that only managed meta-narration or special-token
        # loops has no candidate to offer; unwrap what is usable and let
        # a single surviving draft win outright (saving the critique and
        # vote rounds), or fail the debate so the caller falls back
        drafts = [_clean_candidate(d) for d in drafts]
        drafts = [d for d in drafts if d]
        if not drafts:
            raise ValueError("debate produced no usable candidate")
        if len(drafts) == 1:
            return drafts[0]
        critique = self._generate(
            self._critique(base, drafts), model, timeout, temperature,
            org=org)
        votes = [
            self._generate(self._vote(base, drafts, critique),
                           model, timeout, temperature, org=org)
            for _ in range(2)
        ]
        return self._pick(drafts, votes, critique)

    # -- metering ----------------------------------------------------------
    def _meter(self, org):
        """Fold the token accounting of the last generation into the
        organism's activity counters (llm call + exact ollama tokens)."""
        activity.note(org.store, "llm_calls")
        activity.note(org.store, "prompt_tokens",
                      narration.LAST_CALL_STATS["prompt_tokens"])
        activity.note(org.store, "gen_tokens",
                      narration.LAST_CALL_STATS["gen_tokens"])

    # -- prompts ---------------------------------------------------------
    def _proposal(self, base, which):
        angle = ("" if which == 1
                 else " Take the opposite emotional angle from the first.")
        return (base + "\n\n"
                "Draft a candidate answer, following the task instruction "
                f"above exactly.{angle}")

    def _rogue_proposal(self, base):
        return base + "\n\n" + ROGUE_THOUGHT

    def _critique(self, base, drafts):
        return (base
                + f"\n\nCandidate 1:\n{drafts[0]}"
                + f"\n\nCandidate 2:\n{drafts[1]}"
                + "\n\nAttack both candidates. Point out the weakness in "
                  "each, in one or two sentences. Do not draft a new "
                  "candidate.")

    def _vote(self, base, drafts, critique):
        return (base
                + f"\n\nCandidate 1:\n{drafts[0]}"
                + f"\n\nCandidate 2:\n{drafts[1]}"
                + f"\n\nCritique:\n{critique}"
                + "\n\nWhich candidate is better? Reply exactly with "
                  f"{VOTE_PREFIX}1 or {VOTE_PREFIX}2")

    # -- resolution ------------------------------------------------------
    def _surprise_for(self, chaos):
        """Threshold lookup: the odds of the highest chaos level at or
        below the current chaos apply (monotone, so stress-nudged values
        between the table levels never collapse to the default)."""
        odds = CHAOS_SURPRISE_DEFAULT
        for level, p in sorted(CHAOS_SURPRISE_ODDS.items()):
            if chaos >= level:
                odds = p
        return odds

    def _pick(self, drafts, votes, critique):
        counts = {1: 0, 2: 0}
        for v in votes:
            m = VOTE_RE.search(v)
            if m and m.group(1) in ("1", "2"):
                counts[int(m.group(1))] += 1
        if counts[1] != counts[2]:
            return drafts[0] if counts[1] > counts[2] else drafts[1]
        m = VOTE_RE.search(critique)
        if m and m.group(1) in ("1", "2"):
            return drafts[int(m.group(1)) - 1]
        return self._rng.choice(drafts)

    # -- model -----------------------------------------------------------
    def _generate(self, prompt, model, timeout, temperature, org=None):
        if temperature == 0.0:
            text = narration._ollama_generate(prompt, model, timeout)
            if org is not None:
                self._meter(org)
            return text
        if temperature is None:
            temperature = round(TEMP_MIN + self._rng.random()
                                * (TEMP_MAX - TEMP_MIN), 2)
        text = narration._ollama_generate(prompt, model, timeout,
                                          temperature=temperature)
        if org is not None:
            self._meter(org)
        return text

    def _fallback(self, store, snapshot, user_message, fallback):
        activity.note(store, "fallbacks")
        if fallback is not None:
            return fallback(snapshot)
        if user_message:
            return narration.fallback_respond(snapshot, user_message)
        return narration.fallback_summary(snapshot)
