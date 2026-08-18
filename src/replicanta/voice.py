"""Voice: the organism's public utterance API. Every manifest utterance —
idle thoughts, replies, questions, self-talk, goals, diary entries,
reflections — is assembled here by running the thought arena (arena.py)
over narration.py's prompts and fallbacks. This module is the seam that
keeps the dependency graph acyclic: arena imports narration (prompts),
voice imports both, narration imports neither."""

from replicanta import extensions, narration
from replicanta.arena import ThoughtArena
from replicanta.narration import dedup_emerge, state_snapshot
from replicanta.skills import Skill


def _emerge(
    org,
    task,
    *,
    message=None,
    question=None,
    structured=False,
    fallback=None,
    on_token=None,
    quick=False,
    model=None,
    timeout=None,
    rng=None,
):
    """Build a ThoughtArena and run the requested utterance path.

    All public voice helpers route through here so they only vary
    task-specific arguments instead of repeating the arena construction
    and emerge/quick_take dispatch.
    """
    arena = ThoughtArena(rng=rng, model=model, timeout=timeout)
    method = arena.quick_take if quick else arena.emerge
    return method(
        org,
        task=task,
        user_message=message,
        question=question,
        structured=structured,
        fallback=fallback,
        on_token=on_token,
    )


def narrate(org, model=None, timeout=None, rng=None):
    """First-person idle thought; None when it would just repeat a recent line."""
    return dedup_emerge(
        org, lambda: _emerge(org, task="idle", model=model, timeout=timeout, rng=rng)
    )


def respond(
    org, message, model=None, timeout=None, rng=None, on_token=None, quick=False, record=True
):
    """First-person reply to the user; quick=True skips the debate.

    Records both the incoming message and the generated reply in the
    organism's chat log when ``record=True`` (the default), so direct
    callers retain full two-sided context. Callers that already record
    the message themselves (e.g. ``Organism.hear``) do not see a
    duplicate because the message is only logged if it is not already
    the most recent entry. Group-chat orchestration can pass
    ``record=False`` to keep group lines out of individual chat logs.
    """
    chat_log = getattr(org.store, "chat_log", None)
    if record and (chat_log is None or not chat_log or chat_log[-1] != ["user", message]):
        org.store.record_chat("user", message)
    reply = _emerge(
        org,
        task="reply",
        message=message,
        fallback=lambda snap: narration.fallback_respond(snap, message),
        on_token=on_token,
        quick=quick,
        model=model,
        timeout=timeout,
        rng=rng,
    )
    if record and reply:
        org.store.record_chat("org", reply)
    # Convert empty fallback to None so callers never render a blank reply.
    return reply or None


# -- skills: reflection loop -------------------------------------------------


def reflect(org, model=None, timeout=None, rng=None):
    """One reflection cycle: distill, patch, or 'nothing'; structured."""
    text = _emerge(
        org,
        task="reflect",
        structured=True,
        fallback=lambda _snap: None,
        model=model,
        timeout=timeout,
        rng=rng,
    )
    if text is None:
        return {"action": "none"}
    result = narration.parse_reflect(text)
    if result is None or result["action"] == "none":
        return {"action": "none"}
    if result["action"] == "proposal":
        ok, _reason = extensions.validate(result["entry"])
        if not ok:
            return {"action": "none"}
        entry = extensions.propose(
            org.dir_path / "artifacts" / "extensions.json",
            result["entry"],
            auto_apply=getattr(org.store, "auto_apply_patches", True),
        )
        if entry is not None:
            result["applied"] = entry
        return result
    store = getattr(org, "skills", None)
    if store is None:
        return {"action": "none"}
    if result["action"] == "patched" and store.get(result["name"]) is None:
        result["action"] = "created"
    cycle = org.store.cycle
    store.save(
        Skill(
            name=result["name"],
            when=result["when"],
            how=result["how"],
            created_cycle=cycle,
            updated_cycle=cycle,
        )
    )
    if hasattr(org, "record_self_model"):
        if result["action"] == "patched":
            org.record_self_model(
                f"I refine my skill {result['name']} when {result['when']}"
            )
        else:
            org.record_self_model(f"I tend to {result['name']} when {result['when']}")
    return result


# -- goals --------------------------------------------------------------------


def form_goal(org, model=None, timeout=None, rng=None):
    """One concrete intention grounded in the organism's beliefs."""
    return _emerge(
        org,
        task="form_goal",
        structured=True,
        fallback=lambda snap: narration.fallback_form_goal(snap, rng),
        model=model,
        timeout=timeout,
        rng=rng,
    )


# -- artifacts ----------------------------------------------------------------


def diary_entry(org, model=None, timeout=None, rng=None):
    """One short diary entry about recent days."""
    return _emerge(
        org,
        task="diary",
        structured=True,
        fallback=narration.fallback_diary_entry,
        model=model,
        timeout=timeout,
        rng=rng,
    )


# -- curiosity toward the user -------------------------------------------------


def ask_user(org, model=None, timeout=None, rng=None, on_token=None):
    """One curious question for the user, grounded in a seed."""
    return _emerge(
        org,
        task="ask_user",
        fallback=narration.fallback_ask_user,
        on_token=on_token,
        model=model,
        timeout=timeout,
        rng=rng,
    )


# -- self-talk ----------------------------------------------------------------


def self_ask(org, model=None, timeout=None, rng=None, on_token=None):
    """One self-question, steered away from recent repeats."""
    question = dedup_emerge(
        org,
        lambda: _emerge(
            org,
            task="self_ask",
            fallback=narration.fallback_self_ask,
            on_token=on_token,
            model=model,
            timeout=timeout,
            rng=rng,
        ),
    )
    if question is None:
        question = narration.fallback_self_ask(state_snapshot(org))
    return question


def self_answer(org, message, model=None, timeout=None, rng=None, on_token=None):
    """First-person answer to the organism's own question."""
    answer = dedup_emerge(
        org,
        lambda: _emerge(
            org,
            task="self_answer",
            question=message,
            fallback=lambda snap: narration.fallback_self_answer(snap, message),
            on_token=on_token,
            model=model,
            timeout=timeout,
            rng=rng,
        ),
    )
    if answer is None:
        answer = narration.fallback_self_answer(state_snapshot(org), message)
    return answer


# -- mud companion --------------------------------------------------------------


def mud_decide(org, message, model=None, timeout=None, rng=None, on_token=None):
    """One MUD move chosen by the organism itself; None on failure."""
    return _emerge(
        org,
        task="mud",
        message=message,
        fallback=lambda _snap: None,
        on_token=on_token,
        model=model,
        timeout=timeout,
        rng=rng,
    )
