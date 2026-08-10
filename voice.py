"""Voice: the organism's public utterance API. Every manifest utterance —
idle thoughts, replies, questions, self-talk, goals, diary entries,
reflections — is assembled here by running the thought arena (arena.py)
over narration.py's prompts and fallbacks. This module is the seam that
keeps the dependency graph acyclic: arena imports narration (prompts),
voice imports both, narration imports neither."""

import extensions
import narration
from arena import ThoughtArena
from llmclient import TIMEOUT
from narration import dedup_emerge, state_snapshot
from skills import Skill


def narrate(org, model=None, timeout=TIMEOUT):
    """First-person thought for the organism. Runs the inner arena (two
    proposers and an adversarial critic debate until a majority winner
    emerges) and falls back to a local summary whenever ollama fails.
    Returns None when the only thoughts on offer restate what was just
    said — silence beats an echo."""
    return dedup_emerge(
        org,
        lambda: ThoughtArena().emerge(org, model=model, timeout=timeout))


def respond(org, user_text, model=None, timeout=TIMEOUT, rng=None,
            on_token=None, quick=False):
    """First-person reply to the user. Like everything the organism says,
    the reply runs the inner arena (two proposers draft, an adversarial
    critic attacks, two voters pick) before it manifests — or, with
    quick=True, a single cleaned generation, for many-speaker contexts
    like group chat. The debate itself cannot stream, so the winning
    reply is replayed through on_token in word chunks. Falls back to a
    deterministic reply whenever ollama fails."""
    return ThoughtArena(rng=rng).emerge(
        org, user_message=user_text,
        fallback=lambda snap: narration.fallback_respond(snap, user_text),
        on_token=on_token, model=model, timeout=timeout, quick=quick)


# -- skills: reflection loop -------------------------------------------------

def reflect(org, model=None, timeout=TIMEOUT, rng=None):
    """One reflection cycle: the voice reviews recent experience and
    distills a skill (or patches one, or says 'nothing'). Like every
    utterance, the reflection runs the inner arena — as a structured
    task, so no rogue candidate can break the output format — and the
    winning candidate is parsed and applied to the organism's skill
    store. Offline (or unparseable) is a quiet no-op — never a fake
    skill."""
    text = ThoughtArena(rng=rng).emerge(
        org, prompt_kwargs={"reflect": True}, structured=True,
        fallback=lambda _snap: None, model=model, timeout=timeout)
    if text is None:
        return {"action": "none"}
    result = narration.parse_reflect(text)
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


# -- goals --------------------------------------------------------------------

def form_goal(org, model=None, timeout=TIMEOUT, rng=None):
    """One concrete intention, voiced by the organism and grounded in what
    it knows and remembers. Runs the inner arena as a structured task
    before it manifests. Falls back to a deterministic goal offline."""
    return ThoughtArena(rng=rng).emerge(
        org, prompt_kwargs={"form_goal": True}, structured=True,
        fallback=lambda snap: narration.fallback_form_goal(snap, rng),
        model=model, timeout=timeout)


# -- artifacts ----------------------------------------------------------------

def diary_entry(org, model=None, timeout=TIMEOUT, rng=None):
    """One short diary entry about recent days, voiced by the organism.
    Runs the inner arena as a structured task before it is written.
    Falls back to a deterministic entry offline."""
    return ThoughtArena(rng=rng).emerge(
        org, prompt_kwargs={"diary": True}, structured=True,
        fallback=narration.fallback_diary_entry, model=model,
        timeout=timeout)


# -- curiosity toward the user -------------------------------------------------

def ask_user(org, model=None, timeout=TIMEOUT, rng=None, on_token=None):
    """One curious question directed at the user, grounded in a seed. Runs
    the inner arena before it manifests; the winning question is replayed
    through on_token in word chunks. Falls back to a deterministic
    question whenever ollama is unavailable."""
    return ThoughtArena(rng=rng).emerge(
        org, prompt_kwargs={"ask_user": True},
        fallback=narration.fallback_ask_user, on_token=on_token,
        model=model, timeout=timeout)


# -- self-talk ----------------------------------------------------------------

def self_ask(org, model=None, timeout=TIMEOUT, rng=None, on_token=None):
    """First-person self-question about the organism's own mind, grounded
    in a rotating seed and steered away from its own recent questions.
    Runs the inner arena before it manifests; the winner is replayed
    through on_token in word chunks. Falls back to a deterministic
    template whenever ollama is unavailable — or when the debate can
    only repeat a question it just asked."""
    question = dedup_emerge(
        org,
        lambda: ThoughtArena(rng=rng).emerge(
            org, prompt_kwargs={"self_ask": True},
            fallback=narration.fallback_self_ask, on_token=on_token,
            model=model, timeout=timeout))
    if question is None:
        question = narration.fallback_self_ask(state_snapshot(org))
    return question


def self_answer(org, question, model=None, timeout=TIMEOUT, rng=None,
                on_token=None):
    """First-person answer to the organism's own question. Runs the inner
    arena before it manifests; the winner is replayed through on_token in
    word chunks. Falls back to a deterministic reply whenever ollama is
    unavailable — or when the debate can only restate a recent answer."""
    answer = dedup_emerge(
        org,
        lambda: ThoughtArena(rng=rng).emerge(
            org, prompt_kwargs={"self_question": question},
            fallback=lambda snap: narration.fallback_self_answer(
                snap, question),
            on_token=on_token, model=model, timeout=timeout))
    if answer is None:
        answer = narration.fallback_self_answer(state_snapshot(org),
                                                question)
    return answer
