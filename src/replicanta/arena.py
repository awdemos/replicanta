"""Thought arena: the organism's inner debate. Every utterance the
organism manifests — idle musings, replies, questions to the user,
self-talk, goals, diary entries, reflections — runs through a small
adversarial chamber instead of a single solo model call: two proposers
independently draft a candidate, an adversarial critic attacks both, and
two voters pick a majority winner (or, when the vote deadlocks, the
critic's own preference tips the tie, and a random draw decides a truly
indifferent deadlock). When the local arbiter typed-decision server is
configured (ARBITER_URL), the two voter calls collapse into one batched
typed decision — choice "which candidate responds better" plus noul
"neither is acceptable" — falling back to the voter path on any arbiter
failure or low-confidence answer. The per-round temperature jitters so
the debate is never two identical passes, and in high chaos the organism
may inject a rogue thought of its own (never for structured tasks like
reflections, whose output contract a rogue candidate would break). Any
ollama failure at any stage falls back to the local deterministic
answers, so the organism always has a voice."""

import contextlib
import logging
import json
import os
import random
import re
import urllib.error
from typing import NamedTuple

from replicanta import activity, llmclient, narration, typeddecisions
from replicanta.llmclient import clean_candidate as _clean_candidate

logger = logging.getLogger(__name__)

VOTE_PREFIX = "VOTE: "
VOTE_RE = re.compile(r"(?:^|\b)VOTE\s*[:=-]?\s*([12])\b", re.IGNORECASE)
_LOOSE_VOTE_RE = re.compile(
    r"(?:^|[^\w])(?:candidate|draft|option|choice|the)\s*(?:number\s*)?([12])"
    r"(?:\s*(?:is\s+)?(?:better|stronger|best|wins|prefer\w*|good\w*))?"
    r"(?:[^\w]|$)",
    re.IGNORECASE,
)
_FIRST_SECOND_RE = re.compile(
    r"\b(the\s+)?(?:(?:first|1st)\s+(?:one|candidate|draft)|the\s+first)\b",
    re.IGNORECASE,
)
_SECOND_FIRST_RE = re.compile(
    r"\b(the\s+)?(?:(?:second|2nd)\s+(?:one|candidate|draft)|the\s+second)\b",
    re.IGNORECASE,
)

# chaos -> probability the second proposal is replaced by a rogue
# thought of the organism's own devising. The highest chaos level at or
# below the current (stress-nudged) chaos applies; below 0.3 a small
# default keeps the world from being entirely predictable.
CHAOS_SURPRISE_ODDS = {0.3: 0.05, 0.5: 0.10, 0.7: 0.25, 1.0: 0.50}
CHAOS_SURPRISE_DEFAULT = 0.02

# The rogue thought is a prompt fragment: when it fires, the second
# proposer is replaced by this instruction so the model actually
# generates the rogue thought instead of the draft being a literal.
ROGUE_THOUGHT = (
    "Draft a rogue thought of your own, spun from nowhere - "
    "it may contradict your beliefs, your rules, even "
    "yourself. Keep it to one to three sentences. No "
    "preamble, no quotes, no emoji."
)

TEMP_MIN = 0.7  # lower bound for the per-round temperature jitter
TEMP_MAX = 0.85  # upper bound

# Doom turns stream reasoning before the command line; an 80-token cap
# truncated verbose drafts before the command ever arrived, leaving the
# turn with no move (the model kept 'shooting' into a silent no-op).
QUICK_TAKE_MAX_TOKENS = 160

# arbiter typed-decision vote (see _arbiter_verdict): the winning choice is
# trusted from its probability, the neither candidate statement refuses
# both, and anything short of the bar defers to the LLM voter path
_ARBITER_CHOICE_QID = "better"
_ARBITER_NEITHER_QID = "neither"
ARBITER_VOTE_MIN_PROBABILITY = 0.5
ARBITER_NEITHER_MIN_NOUL = 0.8


class NoUsableCandidateError(ValueError):
    """The model responded but every candidate was unusable — a content
    failure, not a transport failure, so it must not mark the voice
    offline. Subclasses ValueError so existing except-ValueError callers
    still catch it."""


class _Prepped(NamedTuple):
    """Shared setup for one emerge/quick_take call."""

    snapshot: dict
    model: str
    timeout: float
    surprise: float
    temperature: float | None
    voice_online: bool
    build: dict


# candidate cleaning (meta preambles, echoed instructions,
# repetition loops) lives in llmclient; the alias keeps the
# existing import seam for callers and tests.


class ThoughtArena:
    """One debate per utterance. Stateless apart from a per-call RNG, so
    a single instance is safe to share."""

    def __init__(self, rng=None, model=None, timeout=None):
        self._rng = rng if rng is not None else random.Random()  # nosec B311 - simulation RNG, not cryptography
        self._model = model
        self._timeout = timeout

    # -- public ----------------------------------------------------------
    def emerge(
        self,
        org,
        user_message=None,
        task="idle",
        question=None,
        fallback=None,
        structured=False,
        on_token=None,
        temperature=None,
    ):
        """Run a full debate and return the winning candidate.

        task selects the prompt shape (idle, ask_user, self_ask,
        self_answer, form_goal, diary, reflect); question carries the
        self-question for the self_answer task; user_message selects the
        reply task. fallback is a callable taking the state snapshot,
        used when the voice is offline or the debate fails (default: the
        local summary/reply). structured=True suppresses the
        rogue-thought injection so the output contract (e.g. the
        reflection format) survives. The debate itself cannot stream, so
        a winner is replayed through on_token in word chunks to keep the
        incremental display alive.

        Swallowed exceptions: urllib.error.URLError, OSError, and
        RuntimeError are treated as transport failures that mark the voice
        offline and trigger the fallback. json.JSONDecodeError and
        NoUsableCandidateError are treated as content failures; they
        trigger the fallback without marking the voice offline.
        """
        prepped = self._prepare(org, user_message, task, question, temperature, structured)
        if not prepped.voice_online:
            return self._fallback(org.store, prepped.snapshot, user_message, fallback)
        try:
            result = self._debate(
                org,
                prepped.snapshot,
                prepped.build,
                prepped.model,
                prepped.timeout,
                prepped.surprise,
                prepped.temperature,
            )
        except (NoUsableCandidateError, json.JSONDecodeError, urllib.error.URLError, OSError, RuntimeError) as exc:
            return self._settle(org, prepped.snapshot, exc, user_message, fallback)
        return self._settle(org, prepped.snapshot, result, user_message, fallback, on_token=on_token)

    def quick_take(
        self,
        org,
        user_message=None,
        task="idle",
        question=None,
        fallback=None,
        structured=False,
        on_token=None,
        temperature=None,
    ):
        """Single-generation shortcut for many-speaker contexts.

        Runs one cleaned generation instead of the full debate. Exceptions
        and fallback behavior match :meth:`emerge`.
        """
        prepped = self._prepare(org, user_message, task, question, temperature, structured)
        if not prepped.voice_online:
            return self._fallback(org.store, prepped.snapshot, user_message, fallback)
        try:
            result = self._quick_take(
                org,
                prepped.snapshot,
                prepped.build,
                prepped.model,
                prepped.timeout,
                prepped.temperature,
                on_token=on_token,
            )
        except (NoUsableCandidateError, json.JSONDecodeError, urllib.error.URLError, OSError, RuntimeError) as exc:
            return self._settle(org, prepped.snapshot, exc, user_message, fallback)
        return self._settle(org, prepped.snapshot, result, user_message, fallback)

    # -- shared emerge/quick_take plumbing --------------------------------
    def _prepare(self, org, user_message, task, question, temperature, structured):
        """Setup shared by emerge and quick_take: model/timeout resolution,
        the per-utterance bookkeeping that used to hide inside
        state_snapshot (activity digest recording, memory recall marking),
        a private copy of the cached snapshot (the cached dict itself must
        not be mutated), seed rotation, temperature normalization, and the
        voice-online short-circuit."""
        model = self._model or os.environ.get("OLLAMA_MODEL", llmclient.DEFAULT_MODEL)
        timeout = self._timeout or llmclient.default_timeout()
        # record_digest mutates store.activity in place (that module is not
        # lock-aware), so it runs under the store lock: otherwise a save()
        # serializing the same dict concurrently can die mid-iteration.
        lock = getattr(org.store, "_lock", None) or contextlib.nullcontext()
        with lock:
            activity.record_digest(org.store)
        snapshot = dict(narration.state_snapshot(org))
        narration.record_recall(snapshot)
        # every debate circles a different concrete thing — this rotation is
        # what keeps the idle voice from repeating itself; the last few
        # seeds are excluded so a static pool (idle organism) still varies.
        # The history is declared on the organism (Organism.__init__); the
        # getattr fallback covers duck-typed stand-ins in tests.
        recent_seeds = getattr(org, "_recent_seeds", None)
        if recent_seeds is None:
            from collections import deque

            recent_seeds = org._recent_seeds = deque(maxlen=6)
        snapshot["seed"] = llmclient.seed_for(snapshot, self._rng, exclude=recent_seeds)
        recent_seeds.append(snapshot["seed"])
        # the whole organism treats chaos as stress-nudged
        # (organism.chaos_effective()); the arena should too, so surprise
        # rises as the organism gets stressed, not just on the raw knob
        effective = getattr(org, "chaos_effective", lambda: snapshot["chaos"])()
        surprise = 0.0 if structured else self._surprise_for(effective)
        # temperature=0 in the snapshot means deterministic probe mode
        # (tests); anything else jitters per round. Caller-provided
        # temperature overrides the jitter (used for deterministic
        # tool-like generation such as doom commands).
        if temperature is not None:
            temperature = float(temperature)
        elif snapshot.get("temperature") == 0:
            temperature = 0.0
        # voice known-offline: skip the debate entirely so replies stay
        # instant instead of paying an ollama timeout on every utterance
        voice_online = llmclient.voice_online() is not False
        build = {"task": task, "user_message": user_message, "question": question}
        return _Prepped(snapshot, model, timeout, surprise, temperature, voice_online, build)

    def _settle(self, org, snapshot, outcome, user_message, fallback, on_token=None):
        """Result bookkeeping shared by emerge and quick_take: classify a
        failure — content failures (nothing usable, bad payload) fall back
        without touching the voice, transport failures mark it offline —
        or, for a winning candidate, note the success, meter the
        utterance, record skill use, and replay the winner through
        on_token."""
        if isinstance(outcome, (NoUsableCandidateError, json.JSONDecodeError)):
            # content failure (model answered, nothing usable / bad
            # payload) — the voice itself is fine, so don't mark it offline
            return self._fallback(org.store, snapshot, user_message, fallback)
        if isinstance(outcome, (urllib.error.URLError, OSError, RuntimeError)):
            llmclient.note_voice_failure()
            return self._fallback(org.store, snapshot, user_message, fallback)
        if isinstance(outcome, BaseException):
            raise outcome
        result = outcome
        llmclient.note_voice_success()
        activity.note(org.store, "utterances")
        grounded = activity.grounded(snapshot["seed"], result)
        if grounded:
            activity.note(org.store, "grounded_utterances")
        skill_store = getattr(org, "skills", None)
        if skill_store is not None:
            skill_outcome = {
                "grounded": grounded,
                "user_replied": bool(user_message),
                "new_belief": False,
            }
            for skill in snapshot.get("relevant_skills", []):
                skill_store.record_use(skill.name, cycle=org.store.cycle, outcome=skill_outcome)
        if on_token is not None:
            for piece in re.findall(r"\S+\s*", result):
                on_token(piece)
        return result

    # -- debate helpers --------------------------------------------------
    def _quick_take(self, org, snapshot, build, model, timeout, temperature, on_token=None):
        """One proposer, no debate: a single generation cleaned down to
        the candidate. Empty or degenerate output fails the take so the
        caller falls back, exactly like a failed debate."""
        base = narration.build_prompt(snapshot, **build)
        try:
            if on_token is not None:
                # Stream tokens into the UI while generating.
                def _on_token(tok):
                    on_token(tok)

                draft = llmclient.generate_stream(
                    self._proposal(base),
                    model,
                    timeout,
                    temperature=temperature,
                    on_token=_on_token,
                    max_tokens=QUICK_TAKE_MAX_TOKENS,
                )
                draft = _clean_candidate(draft)
            else:
                draft = self._generate(self._proposal(base), model, timeout, temperature, org=org)
                draft = _clean_candidate(draft)
        except Exception:
            logger.exception("_quick_take generate failed")
            raise
        if not draft:
            raise NoUsableCandidateError("quick take produced no usable candidate")
        return draft

    def _debate(self, org, snapshot, build, model, timeout, surprise, temperature):
        base = narration.build_prompt(snapshot, **build)
        drafts = [
            self._generate(self._proposal(base), model, timeout, temperature, org=org),
        ]
        if self._rng.random() < surprise:
            drafts.append(self._generate(self._rogue_proposal(base), model, timeout, temperature, org=org))
        else:
            drafts.append(self._generate(self._proposal(base), model, timeout, temperature, org=org))
        # a proposer that only managed meta-narration or special-token
        # loops has no candidate to offer; unwrap what is usable and let
        # a single surviving draft win outright (saving the critique and
        # vote rounds), or fail the debate so the caller falls back
        drafts = [_clean_candidate(d) for d in drafts]
        drafts = [d for d in drafts if d]
        if not drafts:
            raise NoUsableCandidateError("debate produced no usable candidate")
        if len(drafts) == 1:
            return drafts[0]
        critique = self._generate(self._critique(base, drafts), model, timeout, temperature, org=org)
        verdict = self._arbiter_verdict(base, drafts, critique)
        if verdict == "neither":
            # both candidates fail — the same refusal the arena raises when
            # no usable draft survives (a content failure, so the voice is
            # not marked offline and the caller's fallback answers)
            raise NoUsableCandidateError("arbiter rejected both candidates")
        if verdict in ("a", "b"):
            return drafts[0] if verdict == "a" else drafts[1]
        votes = [
            self._generate(self._vote(base, drafts, critique), model, timeout, temperature, org=org) for _ in range(2)
        ]
        return self._pick(drafts, votes, critique)

    # -- metering ----------------------------------------------------------
    def _meter(self, org, stats):
        """Fold the token accounting of one generation into the organism's
        activity counters (llm call + exact ollama tokens)."""
        activity.note(org.store, "llm_calls")
        activity.note(org.store, "prompt_tokens", stats["prompt_tokens"])
        activity.note(org.store, "gen_tokens", stats["gen_tokens"])

    # -- prompts ---------------------------------------------------------
    def _proposal(self, base):
        return base + "\n\nDraft a candidate answer, following the task instruction above exactly."

    def _rogue_proposal(self, base):
        return base + "\n\n" + ROGUE_THOUGHT

    def _critique(self, base, drafts):
        return (
            base
            + f"\n\nCandidate 1:\n{drafts[0]}"
            + f"\n\nCandidate 2:\n{drafts[1]}"
            + "\n\nAttack both candidates. Point out the weakness in "
            "each, in one or two sentences. Do not draft a new "
            "candidate."
        )

    def _vote(self, base, drafts, critique):
        return (
            base
            + f"\n\nCandidate 1:\n{drafts[0]}"
            + f"\n\nCandidate 2:\n{drafts[1]}"
            + f"\n\nCritique:\n{critique}"
            + "\n\nWhich candidate is better? Reply exactly with "
            f"{VOTE_PREFIX}1 or {VOTE_PREFIX}2"
        )

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

    def _extract_vote(self, text):
        """Return 1 or 2 if the text contains a clear preference, else None.

        Tries the strict VOTE: contract first, then common prose forms
        ("Candidate 1", "the first one", "draft 1 is better")."""
        m = VOTE_RE.search(text)
        if m:
            return int(m.group(1))
        m = _LOOSE_VOTE_RE.search(text)
        if m:
            return int(m.group(1))
        if _FIRST_SECOND_RE.search(text):
            return 1
        if _SECOND_FIRST_RE.search(text):
            return 2
        return None

    def _arbiter_verdict(self, base, drafts, critique):
        """One typed-decision pass replaces the two voter calls: a choice
        over the candidates plus a noul that both are unacceptable.

        Returns "a"/"b" to award the debate, "neither" to refuse both, or
        None to defer to the LLM voter path — arbiter unconfigured, arbiter
        error, a missing/malformed answer, or a winning choice whose
        probability does not clear ARBITER_VOTE_MIN_PROBABILITY.
        """
        if not typeddecisions.enabled():
            return None
        state = (
            base
            + f"\n\nCandidate A (the first candidate):\n{drafts[0]}"
            + f"\n\nCandidate B (the second candidate):\n{drafts[1]}"
            + f"\n\nCritique of both candidates:\n{critique}"
        )
        answers = typeddecisions.decide(
            state,
            {
                _ARBITER_CHOICE_QID: typeddecisions.q_choice(
                    "Which candidate better responds to the task?",
                    {"a": "the first candidate (Candidate A)", "b": "the second candidate (Candidate B)"},
                ),
                _ARBITER_NEITHER_QID: typeddecisions.q_noul("Neither candidate is acceptable; both fail the task."),
            },
        )
        if answers is None:
            return None
        if (typeddecisions.noul_of(answers, _ARBITER_NEITHER_QID) or 0.0) >= ARBITER_NEITHER_MIN_NOUL:
            return "neither"
        picked = typeddecisions.choice_of(answers, _ARBITER_CHOICE_QID)
        if picked is not None and picked[1] >= ARBITER_VOTE_MIN_PROBABILITY and picked[0] in ("a", "b"):
            return picked[0]
        return None

    def _pick(self, drafts, votes, critique):
        counts = {1: 0, 2: 0}
        for v in votes:
            vote = self._extract_vote(v)
            if vote in (1, 2):
                counts[vote] += 1
        if counts[1] != counts[2]:
            return drafts[0] if counts[1] > counts[2] else drafts[1]
        # No majority from voters: ask the critic which candidate it preferred.
        crit = self._extract_vote(critique)
        if crit in (1, 2):
            return drafts[crit - 1]
        return self._rng.choice(drafts)

    # -- model -----------------------------------------------------------
    def _generate(self, prompt, model, timeout, temperature, org=None):
        if temperature is None:
            temperature = round(TEMP_MIN + self._rng.random() * (TEMP_MAX - TEMP_MIN), 2)
            temperature = self._state_scale(temperature, org)
        text, stats = llmclient.generate_with_stats(prompt, model, timeout, temperature=temperature)
        if org is not None:
            self._meter(org, stats)
        return text

    @staticmethod
    def _state_scale(temperature, org):
        """Mental state shapes the sampling itself, not just the prompt
        text: chaos, stress, and insanity widen the jitter band so thought
        genuinely loosens; a calm, rested organism draws slightly tighter
        drafts. Caller-fixed temperatures (doom commands, deterministic
        probes) never pass through the jitter branch and are untouched.
        """
        if org is None:
            return temperature
        try:
            chaos = float(org.chaos_effective())
            stress = float(getattr(org.store, "stress", 0.0) or 0.0)
            insane = bool(getattr(org.store, "insane", False))
        except Exception:  # noqa: BLE001 — duck-typed test doubles
            return temperature
        factor = 1.0 + 0.5 * chaos + 0.25 * stress + (0.45 if insane else 0.0)
        if not insane and chaos < 0.15 and stress < 0.2:
            factor = 0.85  # calm: a notch tighter than the default band
        return round(min(max(temperature * factor, 0.3), 1.25), 2)

    def _fallback(self, store, snapshot, user_message, fallback):
        activity.note(store, "fallbacks")
        if fallback is not None:
            return fallback(snapshot)
        if user_message:
            return narration.fallback_respond(snapshot, user_message)
        return narration.fallback_summary(snapshot)
