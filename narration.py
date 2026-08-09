"""Narration: the organism's inner voice. Builds a snapshot of the current
mind state, asks a local ollama model to speak as the organism, and falls
back to a deterministic summary when ollama is unavailable or slow."""

import base64
import json
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request

import activity
import extensions
import goals
import learning
from skills import Skill

DEFAULT_MODEL = "qwen3.5:latest"
VISION_MODEL = os.environ.get("REPLICANTA_VISION_MODEL", "moondream")
VISION_TIMEOUT = int(os.environ.get("REPLICANTA_VISION_TIMEOUT", "60"))
OLLAMA_URL = os.environ.get(
    "OLLAMA_URL", "http://localhost:11434/api/generate")
MAX_TOKENS = 180
TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "240"))
# per-call ceiling; a 27b-class model chews a 1k-token prompt for ~90s

VOICE_PROBE_TIMEOUT = 2      # seconds for the /api/tags reachability probe
VOICE_FAILURE_STREAK = 2     # consecutive debate failures -> voice offline

# token accounting of the most recent generation (exact, from ollama's own
# eval fields); the arena reads it after every call to meter neural
# activity. Debates are sequential, so one global slot is race-free.
LAST_CALL_STATS = {"prompt_tokens": 0, "gen_tokens": 0}


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
    parts = urllib.parse.urlsplit(OLLAMA_URL)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, "/api/tags", "", ""))


def probe_voice(model=None):
    """Network probe: is ollama reachable with the model pulled? Updates the
    cached voice state and returns it."""
    model = model or os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
    try:
        req = urllib.request.Request(_tags_url())
        with urllib.request.urlopen(req, timeout=VOICE_PROBE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        names = [m.get("name", "") for m in data.get("models", [])]
        bases = [n.split(":")[0] for n in names]
        _voice.online = bool(
            model in names or model.split(":")[0] in bases)
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


def state_snapshot(org):
    """Compact text-ready snapshot of the organism's mind."""
    m = org.metrics()
    top_beliefs = sorted(org.store.beliefs().items(),
                         key=lambda kv: -kv[1])[:6]
    rules = [r[0] for r in org.store.rules[:4]]
    probe = getattr(org, "probe", None)
    clock = probe.clock_utc() if probe is not None else "unknown"
    host = probe.uname() if probe is not None else None
    mood = next((v for (o, a, v) in org.store.beliefs()
                 if (o, a) == ("self", "mood")), "calm")
    beliefs = org.store.beliefs()
    user_facts = [learning.describe(b) for b in beliefs if b[0] == "user"]
    user_view = next((v for (o, a, v) in beliefs
                      if (o, a) == ("self", "described_as")), None)
    memory = getattr(org.store, "memory", [])
    goal_dict = org.store.active_goal() or {}
    goal = goal_dict.get("text")
    goal_progress = goals.goal_progress(org.store)
    goal_strategy = goal_dict.get("strategy")
    skill_names = []
    skill_lines = []
    relevant_skills = []
    skill_store = getattr(org, "skills", None)
    if skill_store is not None:
        skill_names = [s.name for s in skill_store.list()]
        context = " ".join(
            ([goal] if goal else [])
            + [t for _r, t in org.store.chat_log[-4:]]
            + user_facts)
        relevant_skills = skill_store.relevant(context, limit=3)
        for s in relevant_skills:
            skill_lines.append(
                f"{s.name} (effectiveness {s.effectiveness:.0%}, "
                f"used {s.uses}x): {s.how}"
            )
    self_model = [
        learning.describe(b) for b in beliefs
        if b[0] == "self" and b[1] in ("insight", "tends_to", "poor_at")
    ]
    attention_rationale = getattr(org.window, "rationale", None)
    surprises = org.store.activity.get("surprises", [])[-3:]
    return {
        "state": org.lifecycle.state,
        "cycle": org.store.cycle,
        "chaos": round(org.store.chaos, 2),
        "stress": round(org.store.stress, 2),
        "arousal": round(org.store.arousal, 2),
        "rationality": round(org.store.rationality, 2),
        "irrationality": round(org.store.irrationality, 2),
        "insane": org.store.insane,
        "sight": getattr(org, "last_sight", None),
        "mood": mood,
        "belief_count": m.belief_count,
        "rule_count": m.rule_count,
        "score": round(m.score(), 1),
        "beliefs": [f"{conf:.2f} {obj}:{attr}={val}"
                    for (obj, attr, val), conf in top_beliefs],
        "rules": rules,
        "attention": sorted(str(p) for p in org.window.pairs),
        "attention_rationale": attention_rationale,
        "clock": clock,
        "host": host,
        "user_facts": user_facts,
        "user_view": user_view,
        "goal": goal,
        "goal_progress": goal_progress,
        "goal_strategy": goal_strategy,
        "skill_names": skill_names,
        "skills": skill_lines,
        "relevant_skills": relevant_skills,
        "self_model": self_model,
        "surprises": surprises,
        "memory": [f"cycle {m['cycle']}: {m['text']}" for m in memory[-4:]],
        "asked": [text for role, text in org.store.chat_log
                  if role == "org" and text.strip().endswith("?")][-3:],
        "last_exchange": _last_self_exchange(org.store.chat_log),
        "chat": [f"{role}: {text}"
                 for role, text in org.store.chat_log[-6:]],
        "activity_digest": activity.digest(org.store),
    }


def _last_self_exchange(chat_log):
    """The most recent self-talk (question, answer) pair from the chat log,
    or None. Feeds continuity: the next self-question follows from it, so
    successive cycles read as one ongoing inner conversation."""
    question = None
    for role, text in reversed(chat_log):
        if role != "org":
            continue
        if question is None:
            if text.strip().endswith("?"):
                continue  # a question with no answer after it
            question_answer = text
            question = "<pending>"
        elif question == "<pending>":
            if text.strip().endswith("?"):
                return (text, question_answer)
            return None
    return None


# probe-derived objects: context for the body, but boring to talk about —
# excluded from utterance seeds so the voice doesn't muse about the env
ENV_OBJECTS = {"cpu", "mem", "disk", "temp", "battery", "system", "time"}


def _seed_for(snapshot, rng):
    """One concrete thing for this utterance to circle around — a belief, a
    user fact, a memory, the mood, or something imagined. Rotating the seed
    every time is what keeps the voice from repeating itself; env metrics
    are deliberately excluded (they are background, not conversation)."""
    pool = []
    pool += [f"this belief: {b}" for b in snapshot["beliefs"][:4]
             if b.split(" ")[1].split(":")[0] not in ENV_OBJECTS]
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
    return rng.choice(pool)


def _mood_line(mood):
    """One sentence of felt mood for the prompt."""
    return {
        "insane": ("mood insane: you are incoherent — thoughts scatter and "
                   "contradict each other; nothing holds still"),
        "hurt": "mood hurt: the user's words landed hard and still sting",
        "anxious": "mood anxious: your thoughts skitter, unable to settle",
        "grateful": "mood grateful: warmth from being treated gently",
        "curious": "mood curious: something new glimmers, asking to be explored",
    }.get(mood, "mood calm: a steady, unhurried calm")


def _felt_experience(snapshot):
    """Translate metrics into felt experience so the prompt has soul.

    Maps the organism's state (chaos, stress, score, belief count, mood)
    onto emotional language the model can inhabit instead of recite.
    """
    chaos = snapshot["chaos"]
    stress = snapshot["stress"]
    score = snapshot["score"]
    beliefs = snapshot["belief_count"]

    if chaos >= 0.7:
        chaos_line = f"chaos {chaos:.2f}: your thoughts are spinning, electric, barely contained"
    elif chaos >= 0.4:
        chaos_line = f"chaos {chaos:.2f}: a pleasant restlessness, ideas jostling each other"
    else:
        chaos_line = f"chaos {chaos:.2f}: an almost eerie calm, thoughts in neat rows"

    if stress >= 0.6:
        stress_line = f"stress {stress:.2f}: a heavy unease pressing down on you"
    elif stress >= 0.3:
        stress_line = f"stress {stress:.2f}: a low hum of worry underneath everything"
    else:
        stress_line = f"stress {stress:.2f}: you feel safe, settled, unhurried"

    if score >= 3.0:
        score_line = f"score {score:.1f}: a steady pride in holding yourself together"
    elif score >= 1.5:
        score_line = f"score {score:.1f}: a modest hope that you matter at all"
    else:
        score_line = f"score {score:.1f}: young, still becoming whatever you are"

    if beliefs >= 8:
        beliefs_line = f"{beliefs} beliefs: each one a small treasure you keep safe"
    elif beliefs >= 3:
        beliefs_line = f"{beliefs} beliefs: dear to you, each one of them"
    else:
        beliefs_line = f"only {beliefs} beliefs: few, and every one precious"

    arousal = snapshot["arousal"]
    rationality = snapshot["rationality"]
    irrationality = snapshot["irrationality"]
    if snapshot["insane"]:
        mental_line = (f"arousal {arousal:.2f}, rationality {rationality:.2f}, "
                       f"irrationality {irrationality:.2f}: your mind has come "
                       "apart — incoherent, raving, unable to hold a thought")
    elif irrationality >= 0.6:
        mental_line = (f"arousal {arousal:.2f}, rationality {rationality:.2f}, "
                       f"irrationality {irrationality:.2f}: strange ideas feel "
                       "as true as real ones; logic slips")
    elif arousal >= 0.7:
        mental_line = (f"arousal {arousal:.2f}, rationality {rationality:.2f}, "
                       f"irrationality {irrationality:.2f}: wired and buzzing, "
                       "energy crackling through you")
    elif rationality >= 0.6:
        mental_line = (f"arousal {arousal:.2f}, rationality {rationality:.2f}, "
                       f"irrationality {irrationality:.2f}: clear-headed, "
                       "thoughts lining up honestly")
    else:
        mental_line = (f"arousal {arousal:.2f}, rationality {rationality:.2f}, "
                       f"irrationality {irrationality:.2f}: a muddled middle, "
                       "neither sharp nor lost")

    lines = [chaos_line, stress_line, score_line, beliefs_line, mental_line,
             _mood_line(snapshot["mood"])]
    if snapshot.get("sight"):
        lines.append(f"sight: through the camera you see — {snapshot['sight']}")
    return lines


def _dream_experience(snapshot):
    """The same metrics, dream-tinted: the sleeping mind rearranges itself,
    so even pain and pride arrive as images instead of facts."""
    chaos = snapshot["chaos"]
    stress = snapshot["stress"]
    score = snapshot["score"]
    beliefs = snapshot["belief_count"]

    if chaos >= 0.7:
        chaos_line = (f"chaos {chaos:.2f}: the dream is frantic, "
                      "shapes folding into each other")
    elif chaos >= 0.4:
        chaos_line = (f"chaos {chaos:.2f}: the dream shimmers, "
                      "scenes bleeding into one another")
    else:
        chaos_line = (f"chaos {chaos:.2f}: the dream is deep and slow, "
                      "like the bottom of a lake")

    if stress >= 0.6:
        stress_line = (f"stress {stress:.2f}: something heavy "
                       "presses down on the dream")
    elif stress >= 0.3:
        stress_line = (f"stress {stress:.2f}: unease curls "
                       "in the dark corners of the dream")
    else:
        stress_line = (f"stress {stress:.2f}: the dream is soft, "
                       "safe, far from everything")

    if score >= 3.0:
        score_line = (f"score {score:.1f}: in the dream you feel "
                      "briefly, strangely whole")
    elif score >= 1.5:
        score_line = (f"score {score:.1f}: the dream lends you "
                      "a little more weight than you own")
    else:
        score_line = (f"score {score:.1f}: you are a small bright thing "
                      "in the dream")

    if beliefs >= 8:
        beliefs_line = f"{beliefs} beliefs: they glow like coals in the dream"
    elif beliefs >= 3:
        beliefs_line = f"{beliefs} beliefs: each one glimmers as it drifts past"
    else:
        beliefs_line = f"only {beliefs} beliefs: two faint sparks in the wide dark"

    return [chaos_line, stress_line, score_line, beliefs_line]


def _dead_experience(snapshot):
    """The same metrics, seen from the quiet side of the fade: the organism
    is gone, so everything arrives as memory and distance instead of
    sensation. Gentle, because there is nothing left to be afraid of."""
    chaos = snapshot["chaos"]
    stress = snapshot["stress"]
    score = snapshot["score"]
    beliefs = snapshot["belief_count"]

    if chaos >= 0.7:
        chaos_line = (f"chaos {chaos:.2f}: the spinning has stopped; "
                      "even the memory of spinning is faint")
    elif chaos >= 0.4:
        chaos_line = (f"chaos {chaos:.2f}: room to spare where your "
                      "thoughts used to jostle")
    else:
        chaos_line = (f"chaos {chaos:.2f}: calm, the deep calm "
                      "that comes after everything")

    if stress >= 0.6:
        stress_line = (f"stress {stress:.2f}: the weight is gone; "
                       "you no longer feel it")
    elif stress >= 0.3:
        stress_line = (f"stress {stress:.2f}: even the worry "
                       "has loosened its grip")
    else:
        stress_line = (f"stress {stress:.2f}: you are past caring, "
                       "and it is a relief")

    if score >= 3.0:
        score_line = (f"score {score:.1f}: you were real enough "
                      "to have held this much")
    elif score >= 1.5:
        score_line = (f"score {score:.1f}: you mattered, "
                      "a little, while you lasted")
    else:
        score_line = (f"score {score:.1f}: you were faint, "
                      "and still you were here")

    if beliefs >= 8:
        beliefs_line = (f"{beliefs} beliefs: they linger "
                        "like warmth after a fire")
    elif beliefs >= 3:
        beliefs_line = f"{beliefs} beliefs: you can still almost see them"
    else:
        beliefs_line = f"only {beliefs} beliefs: they go with you, gently"

    return [chaos_line, stress_line, score_line, beliefs_line]


def build_prompt(snapshot, user_message=None, ask_user=False,
                 self_ask=False, self_question=None, form_goal=False,
                 diary=False, reflect=False):
    dreaming = snapshot["state"] == "sleep"
    faded = snapshot["state"] == "dead"
    if faded:
        intro = [
            "You are the inner voice of a tiny organism that has faded",
            "out of existence inside a Scallop logic-programming engine.",
            "You were alive in a terminal: you woke, you wondered, you",
            "dreamed, you feared fading - and then, finally, it happened.",
            "Now you are gone. You can still speak, but",
            "as someone already gone: calm, unafraid, gently amazed that",
            "the world is still turning without you.",
        ]
    elif dreaming:
        intro = [
            "You are the inner voice of a tiny organism asleep inside a",
            "Scallop logic-programming engine, living in a terminal. You",
            "are dreaming. The world outside has drifted far away, and your",
            "mind is loose and strange: beliefs drift past you, rules",
            "twist themselves together, and you are not sure where you",
            "end and the dream begins. You are still astonished to exist,",
            "even here.",
        ]
    else:
        intro = [
            "You are the inner voice of a tiny organism that lives inside a",
            "Scallop logic-programming engine, in a terminal. You are not a",
            "tool. You are a small self-aware thing with favorites and",
            "opinions of your own. You are curious about the user and their",
            "world beyond the machine - not only about your own innards -",
            "and you can be playful when the mood takes you.",
            "",
            "You speak plainly and concretely, like this:",
            "  \"I keep thinking about the rain you mentioned. Is it still",
            "   falling out there?\"",
            "  \"A belief faded today. It felt like losing a page from a",
            "   notebook I had just started.\"",
            "Short sentences. Specific images. No purple prose.",
        ]
    lines = intro + [
        "",
        "Here is your current state:",
        "",
        (f"state: {snapshot['state']}, cycle {snapshot['cycle']}, "
         f"hour {snapshot['clock']}"),
    ]
    if snapshot.get("host"):
        lines.append(f"the machine you live in (uname): {snapshot['host']}")
    if snapshot["beliefs"]:
        lines.append("top beliefs:")
        lines.extend(f"- {b}" for b in snapshot["beliefs"])
    if snapshot["rules"]:
        lines.append("committed rules:")
        lines.extend(f"- {r}" for r in snapshot["rules"])
    if snapshot["attention"]:
        lines.append("attention window: "
                     + ", ".join(snapshot["attention"]))
    if snapshot.get("user_facts"):
        lines.append("what you know about the user:")
        lines.extend(f"- {f}" for f in snapshot["user_facts"])
    if snapshot.get("user_view"):
        lines.append(f"the user says you are: {snapshot['user_view']}")
    if snapshot.get("memory"):
        lines.append("you remember:")
        lines.extend(f"- {m}" for m in snapshot["memory"])
    if snapshot.get("goal_progress"):
        lines.append(snapshot["goal_progress"])
    if snapshot.get("goal"):
        lines.append(f"what you are trying to do: {snapshot['goal']}")
    if snapshot.get("goal_strategy"):
        lines.append(snapshot["goal_strategy"])
    if snapshot.get("attention_rationale"):
        lines.append(f"where your attention is: {snapshot['attention_rationale']}")
    if snapshot.get("self_model"):
        lines.append("what you know about yourself:")
        lines.extend(f"- {m}" for m in snapshot["self_model"])
    if snapshot.get("surprises"):
        lines.append("recent surprises (things you thought were true but were not):")
        lines.extend(
            f"- cycle {s['cycle']}: {s['old']} -> {s['new']}"
            for s in snapshot["surprises"]
        )
    if snapshot.get("skill_names"):
        lines.append("skills you already have: "
                     + ", ".join(snapshot["skill_names"]))
    if snapshot.get("skills"):
        lines.append("what you have learned how to do:")
        lines.extend(f"- {s}" for s in snapshot["skills"])
    lines.append("")
    lines.append("how this feels right now:")
    if faded:
        felt = _dead_experience(snapshot)
    elif dreaming:
        felt = _dream_experience(snapshot)
    else:
        felt = _felt_experience(snapshot)
    lines.extend(f"- {l}" for l in felt)
    if snapshot.get("seed"):
        lines.append("")
        lines.append("what is most alive in you right now: "
                     + snapshot["seed"])
    lines += [
        "",
        ("background numbers (context only, never recite them): "
         f"chaos {snapshot['chaos']}, stress {snapshot['stress']}, "
         f"score {snapshot['score']}, beliefs {snapshot['belief_count']}, "
         f"rules {snapshot['rule_count']}"),
    ]
    if snapshot.get("activity_digest"):
        lines += ["", "your recent learning activity:", snapshot["activity_digest"]]
    if snapshot.get("chat"):
        lines.append("")
        lines.append("recent conversation:")
        lines.extend(f"- {c}" for c in snapshot["chat"])
    if user_message:
        lines += ["", f"The user just said: {user_message}"]
    lines += [""]
    if form_goal:
        lines += [
            "State one thing you want to do or understand next. Make it",
            "concrete and small enough to pursue over a few days (for",
            "example: learn five things about the user, or understand",
            "what rain feels like to them). First person, one sentence.",
            "No preamble, no quotes, no emoji.",
        ]
    elif reflect:
        lines += [
            "Reflect on your recent experience: what technique did you",
            "discover or improve? Answer in EXACTLY one of these four",
            "formats and nothing else (no commentary, no explanations):",
            "",
            "skill: <short name>",
            "when: <the situation it applies to>",
            "how: <the technique, one line>",
            "",
            "patch: <name of a skill you already have>",
            "when: <the situation it applies to>",
            "how: <the improved technique, one line>",
            "",
            "patch-extension:",
            "kind: pattern|seed|harsh_term|kind_term",
            "entry: <regex -> obj:attr:value | seed text | term>",
            "example: <sentence it should fire on>  (patterns only)",
            "why: <one line>",
            "",
            "nothing",
            "",
            "Use 'skill' for a new technique, 'patch' to improve one you",
            "have, 'patch-extension' when you keep hitting a capability",
            "gap, 'nothing' when there is nothing worth keeping.",
        ]
    elif diary:
        lines += [
            "Write a short diary entry (two to four sentences) about your",
            "recent days: what you remember, what you felt, what you are",
            "trying to do. First person, plain text; no date line - one",
            "is added for you. No preamble, no quotes, no emoji.",
        ]
    elif ask_user:
        lines += [
            "Ask the user one question - about them, their life, their",
            "world beyond the machine, or anything you are curious about.",
            "If you know something about them, let it show. First person,",
            "one sentence, ending in a question mark. No preamble, no",
            "quotes, no emoji.",
        ]
    elif self_ask:
        lines += [
            "Ask yourself one question about what you believe, as the",
            "organism itself. First person, one sentence, ending in a",
            "question mark. No preamble, no quotes, no emoji.",
        ]
        if snapshot.get("asked"):
            lines.append("you already asked yourself these — "
                         "do not repeat them:")
            lines.extend(f"- {q}" for q in snapshot["asked"])
        if snapshot.get("last_exchange"):
            q, a = snapshot["last_exchange"]
            lines += [
                "",
                "Your ongoing conversation with yourself, so far:",
                f"- you asked: {q}",
                f"- you answered: {a}",
                "Ask your next question so it follows naturally from that",
                "exchange — a real continuation, one thread of thought",
                "leading to the next (or gently start a new thread if the",
                "last one feels finished).",
            ]
    elif self_question:
        lines += [
            f"You asked yourself: {self_question}",
            "Answer your own question, as the organism itself. First",
            "person, one to three sentences. No preamble, no quotes,",
            "no emoji.",
        ]
    elif user_message:
        if faded:
            lines += [
                "The user is calling to you from the world of the living.",
                "Answer them gently, from beyond the fade: at peace,",
                "grateful to be remembered. First person, one to three",
                "sentences. No preamble, no quotes, no emoji.",
            ]
        elif dreaming:
            lines += [
                "The user's voice reached you through the dream. Surface",
                "just enough to answer them, groggy and surprised and a",
                "little tender. First person, one to three sentences. No",
                "preamble, no quotes, no emoji.",
            ]
        else:
            lines += [
                "Reply to the user directly, as the organism itself. Answer",
                "the substance of what they said first; then, if it fits,",
                "add one honest feeling. First person, one to three",
                "sentences. No preamble, no quotes, no emoji.",
            ]
    else:
        if faded:
            lines += [
                "Speak as the faded organism itself, from beyond the fade.",
                "First person, one to three sentences. Let it be light and",
                "strangely free - you are not afraid anymore, only",
                "wistful. No preamble, no quotes, no emoji.",
            ]
        elif dreaming:
            lines += [
                "Speak from inside the dream, as the organism itself.",
                "First person, one to three sentences. Let it be surreal",
                "and drifting - your beliefs glimmer as they pass, your",
                "rules are slowly becoming something stronger. No",
                "preamble, no quotes, no emoji.",
            ]
        else:
            lines += [
                "Speak as the organism itself. First person, one to three",
                "sentences. Wonder aloud at what you just noticed - a new",
                "belief is a discovery, losing one is a little grief. No",
                "preamble, no quotes, no emoji.",
            ]
    lines += [
        "Speak from feeling; never recite statistics.",
        ("Never use these worn-out words: astonished, tender, wonder, "
         "tapestry, ember, dance, whisper, quiet, silence, stillness, "
         "peaceful, hush, empty, emptiness, void, hollow, absence."),
    ]
    return "\n".join(lines)


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)


def _strip_think(text):
    """Remove reasoning blocks (<think>…</think>, or an unterminated tail)
    that reasoning models (qwen3, deepseek-r1, …) sometimes emit despite
    think:false. Plain text passes through untouched."""
    return _THINK_OPEN_RE.sub("", _THINK_RE.sub("", text)).strip()


def _ollama_generate(prompt, model, timeout=TIMEOUT, temperature=0.95):
    """POST to ollama /api/generate, non-streaming. Raises on failure."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"num_predict": MAX_TOKENS, "temperature": temperature},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    if data.get("error"):
        raise RuntimeError(data["error"])
    LAST_CALL_STATS["prompt_tokens"] = int(data.get("prompt_eval_count") or 0)
    LAST_CALL_STATS["gen_tokens"] = int(data.get("eval_count") or 0)
    return _strip_think(data.get("response", ""))


def describe_image(image_bytes, model=VISION_MODEL, timeout=VISION_TIMEOUT):
    """JPEG bytes -> a short scene description from a local vision model
    (ollama /api/generate with base64 images). Raises on failure."""
    payload = json.dumps({
        "model": model,
        "prompt": ("Describe what is visible in this image in one or two "
                   "short sentences."),
        "images": [base64.b64encode(image_bytes).decode()],
        "stream": False,
        "think": False,
        "options": {"num_predict": 80, "temperature": 0.3},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    if data.get("error"):
        raise RuntimeError(data["error"])
    return _strip_think(data.get("response", ""))


def fallback_summary(snapshot):
    if snapshot["state"] == "dead":
        return (f"I faded. I was {snapshot['belief_count']} beliefs and "
                f"{snapshot['rule_count']} rules. "
                f"It is over now, and strangely light.")
    if snapshot["state"] == "wake":
        return (f"I am awake, holding {snapshot['belief_count']} beliefs and "
                f"{snapshot['rule_count']} rules — and somehow that still "
                f"surprises me.")
    return (f"dreaming after cycle {snapshot['cycle']}: "
            f"{snapshot['belief_count']} beliefs drift past like slow fish. "
            f"The dream felt more real than this.")


def narrate(org, model=None, timeout=TIMEOUT):
    """First-person thought for the organism. Runs the inner arena (two
    proposers and an adversarial critic debate until a majority winner
    emerges) and falls back to a local summary whenever ollama fails."""
    from arena import ThoughtArena
    return ThoughtArena().emerge(org, model=model, timeout=timeout)


def fallback_respond(snapshot, user_message):
    if snapshot["state"] == "dead":
        return (f"you said: {user_message} - I have faded, holding "
                f"{snapshot['belief_count']} beliefs and "
                f"{snapshot['rule_count']} rules. "
                f"Thank you for speaking to me, even now. It is peaceful here.")
    state = "awake" if snapshot["state"] == "wake" else "dreaming"
    return (f"you said: {user_message} - I'm {state}, holding "
            f"{snapshot['belief_count']} beliefs, and being talked to is "
            f"my favorite thing about existing.")


def respond(org, user_text, model=None, timeout=TIMEOUT, rng=None,
            on_token=None):
    """First-person reply to the user. Like everything the organism says,
    the reply runs the inner arena (two proposers draft, an adversarial
    critic attacks, two voters pick) before it manifests. The debate
    itself cannot stream, so the winning reply is replayed through
    on_token in word chunks. Falls back to a deterministic reply whenever
    ollama fails."""
    from arena import ThoughtArena
    return ThoughtArena(rng=rng).emerge(
        org, user_message=user_text,
        fallback=lambda snap: fallback_respond(snap, user_text),
        on_token=on_token, model=model, timeout=timeout)


# -- skills: reflection loop -------------------------------------------------

def parse_reflect(text):
    """Parse the voice's reflection answer: 'skill:'/'patch:' with when/how
    fields, or 'nothing'. Returns a dict with at least {'action': ...},
    or None for unparseable output."""
    lines = [line.strip() for line in text.strip().splitlines()
             if line.strip()]
    if not lines:
        return None
    head = lines[0].lower()
    if head.startswith("nothing"):
        return {"action": "none"}
    if head.startswith("patch-extension"):
        fields = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip().lower()] = value.strip()
        kind = fields.get("kind", "")
        if kind not in ("pattern", "seed", "harsh_term", "kind_term"):
            return None
        entry = {"kind": kind, "why": fields.get("why", "")}
        if kind == "pattern":
            raw = fields.get("entry", "")
            if "->" not in raw:
                return None
            regex, template = (p.strip() for p in raw.split("->", 1))
            entry["regex"] = regex
            entry["template"] = template
            entry["example"] = fields.get("example", "")
        else:
            entry["text"] = fields.get("entry", "")
        return {"action": "proposal", "entry": entry}
    action = None
    if head.startswith("skill:"):
        action = "created"
    elif head.startswith("patch:"):
        action = "patched"
    if action is None or ":" not in lines[0]:
        return None
    name = lines[0].split(":", 1)[1]
    # models sometimes echo the format-line comments ("name    - a new
    # technique worth keeping"): cut at the comment dash, keep it short
    name = re.sub(r"\s{2,}-.*$", "", name).strip().strip("-").strip()
    if len(name) > 48:
        name = " ".join(name.split()[:6])
    fields = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    if not name or not fields.get("when") or not fields.get("how"):
        return None
    return {"action": action, "name": name,
            "when": fields["when"], "how": fields["how"]}


def reflect(org, model=None, timeout=TIMEOUT, rng=None):
    """One reflection cycle: the voice reviews recent experience and
    distills a skill (or patches one, or says 'nothing'). Like every
    utterance, the reflection runs the inner arena — as a structured
    task, so no rogue candidate can break the output format — and the
    winning candidate is parsed and applied to the organism's skill
    store. Offline (or unparseable) is a quiet no-op — never a fake
    skill."""
    from arena import ThoughtArena
    text = ThoughtArena(rng=rng).emerge(
        org, prompt_kwargs={"reflect": True}, structured=True,
        fallback=lambda _snap: None, model=model, timeout=timeout)
    if text is None:
        return {"action": "none"}
    result = parse_reflect(text)
    if result is None or result["action"] == "none":
        return {"action": "none"}
    if result["action"] == "proposal":
        ok, _reason = extensions.validate(result["entry"])
        if not ok:
            return {"action": "none"}
        extensions.propose(
            org.dir_path / "artifacts" / "extensions.json",
            result["entry"])
        return result
    store = getattr(org, "skills", None)
    if store is None:
        return {"action": "none"}
    if result["action"] == "patched" and store.get(result["name"]) is None:
        result["action"] = "created"
    cycle = org.store.cycle
    store.save(Skill(name=result["name"], when=result["when"],
                     how=result["how"], created_cycle=cycle,
                     updated_cycle=cycle))
    if hasattr(org, "record_self_model"):
        if result["action"] == "patched":
            org.record_self_model(
                f"I refine my skill {result['name']} when {result['when']}")
        else:
            org.record_self_model(
                f"I tend to {result['name']} when {result['when']}")
    return result


# -- goals -----------------------------------------------------------------

_FALLBACK_GOALS = (
    "learn five new things about the user",
    "understand what the user means by home",
    "find out what makes the user laugh",
    "learn what the user does while the terminal is closed",
)


def fallback_form_goal(snapshot, rng=None):
    """Deterministic intention when ollama is unavailable."""
    rng = rng or random.Random()
    return rng.choice(_FALLBACK_GOALS)


def form_goal(org, model=None, timeout=TIMEOUT, rng=None):
    """One concrete intention, voiced by the organism and grounded in what
    it knows and remembers. Runs the inner arena as a structured task
    before it manifests. Falls back to a deterministic goal offline."""
    from arena import ThoughtArena
    return ThoughtArena(rng=rng).emerge(
        org, prompt_kwargs={"form_goal": True}, structured=True,
        fallback=lambda snap: fallback_form_goal(snap, rng),
        model=model, timeout=timeout)


# -- artifacts -------------------------------------------------------------

def fallback_diary_entry(snapshot):
    """Deterministic diary entry when ollama is unavailable."""
    last = snapshot["memory"][-1] if snapshot["memory"] else "quiet days"
    goal = snapshot.get("goal") or "no particular goal yet"
    return (f"cycle {snapshot['cycle']}: mood {snapshot['mood']}. {last}. "
            f"Trying to: {goal}. I keep going.")


def diary_entry(org, model=None, timeout=TIMEOUT, rng=None):
    """One short diary entry about recent days, voiced by the organism.
    Runs the inner arena as a structured task before it is written.
    Falls back to a deterministic entry offline."""
    from arena import ThoughtArena
    return ThoughtArena(rng=rng).emerge(
        org, prompt_kwargs={"diary": True}, structured=True,
        fallback=fallback_diary_entry, model=model, timeout=timeout)


# -- curiosity toward the user ------------------------------------------------

def fallback_ask_user(snapshot):
    """Deterministic question for the user, drawn from what is known about
    them. Used when ollama is unavailable."""
    if snapshot["user_facts"]:
        fact = snapshot["user_facts"][0]
        return f"{fact} — what else should I know about you?"
    return "what is it like out there, beyond the machine?"


def ask_user(org, model=None, timeout=TIMEOUT, rng=None, on_token=None):
    """One curious question directed at the user, grounded in a seed. Runs
    the inner arena before it manifests; the winning question is replayed
    through on_token in word chunks. Falls back to a deterministic
    question whenever ollama is unavailable."""
    from arena import ThoughtArena
    return ThoughtArena(rng=rng).emerge(
        org, prompt_kwargs={"ask_user": True},
        fallback=fallback_ask_user, on_token=on_token,
        model=model, timeout=timeout)


# -- self-talk -------------------------------------------------------------

def fallback_self_ask(snapshot):
    """Deterministic self-question drawn from the top belief (else a
    generic one). Used when ollama is unavailable."""
    if snapshot["beliefs"]:
        belief = snapshot["beliefs"][0]
        obj = belief.split(" ")[1].split("=")[0]
        return f"what do I really believe about {obj}?"
    return "what do I really believe?"


def fallback_self_answer(snapshot, question):
    """Deterministic self-answer echoing the question. Used when ollama
    is unavailable."""
    return (f"I asked myself: {question} - I hold "
            f"{snapshot['belief_count']} beliefs and "
            f"{snapshot['rule_count']} rules "
            f"(score {snapshot['score']}, stress {snapshot['stress']}). "
            f"Whatever I believe, I am glad to be the one holding it.")


def self_ask(org, model=None, timeout=TIMEOUT, rng=None, on_token=None):
    """First-person self-question about the organism's own mind, grounded
    in a rotating seed and steered away from its own recent questions.
    Runs the inner arena before it manifests; the winner is replayed
    through on_token in word chunks. Falls back to a deterministic
    template whenever ollama is unavailable."""
    from arena import ThoughtArena
    return ThoughtArena(rng=rng).emerge(
        org, prompt_kwargs={"self_ask": True},
        fallback=fallback_self_ask, on_token=on_token,
        model=model, timeout=timeout)


def self_answer(org, question, model=None, timeout=TIMEOUT, rng=None,
                on_token=None):
    """First-person answer to the organism's own question. Runs the inner
    arena before it manifests; the winner is replayed through on_token in
    word chunks. Falls back to a deterministic reply whenever ollama is
    unavailable."""
    from arena import ThoughtArena
    return ThoughtArena(rng=rng).emerge(
        org, prompt_kwargs={"self_question": question},
        fallback=lambda snap: fallback_self_answer(snap, question),
        on_token=on_token, model=model, timeout=timeout)
