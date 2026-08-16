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


def narrate(org, model=None, timeout=None, rng=None):
    """First-person idle thought; None when it would just repeat a recent line."""
    return dedup_emerge(
        org, lambda: ThoughtArena(rng=rng).emerge(org, model=model, timeout=timeout)
    )


def respond(
    org, user_text, model=None, timeout=None, rng=None, on_token=None, quick=False
):
    """First-person reply to the user; quick=True skips the debate."""
    return ThoughtArena(rng=rng).emerge(
        org,
        user_message=user_text,
        fallback=lambda snap: narration.fallback_respond(snap, user_text),
        on_token=on_token,
        model=model,
        timeout=timeout,
        quick=quick,
    )


# -- skills: reflection loop -------------------------------------------------


def reflect(org, model=None, timeout=None, rng=None):
    """One reflection cycle: distill, patch, or 'nothing'; structured."""
    text = ThoughtArena(rng=rng).emerge(
        org,
        task="reflect",
        structured=True,
        fallback=lambda _snap: None,
        model=model,
        timeout=timeout,
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
    return ThoughtArena(rng=rng).emerge(
        org,
        task="form_goal",
        structured=True,
        fallback=lambda snap: narration.fallback_form_goal(snap, rng),
        model=model,
        timeout=timeout,
    )


# -- artifacts ----------------------------------------------------------------


def diary_entry(org, model=None, timeout=None, rng=None):
    """One short diary entry about recent days."""
    return ThoughtArena(rng=rng).emerge(
        org,
        task="diary",
        structured=True,
        fallback=narration.fallback_diary_entry,
        model=model,
        timeout=timeout,
    )


# -- curiosity toward the user -------------------------------------------------


def ask_user(org, model=None, timeout=None, rng=None, on_token=None):
    """One curious question for the user, grounded in a seed."""
    return ThoughtArena(rng=rng).emerge(
        org,
        task="ask_user",
        fallback=narration.fallback_ask_user,
        on_token=on_token,
        model=model,
        timeout=timeout,
    )


# -- self-talk ----------------------------------------------------------------


def self_ask(org, model=None, timeout=None, rng=None, on_token=None):
    """One self-question, steered away from recent repeats."""
    question = dedup_emerge(
        org,
        lambda: ThoughtArena(rng=rng).emerge(
            org,
            task="self_ask",
            fallback=narration.fallback_self_ask,
            on_token=on_token,
            model=model,
            timeout=timeout,
        ),
    )
    if question is None:
        question = narration.fallback_self_ask(state_snapshot(org))
    return question


def self_answer(org, question, model=None, timeout=None, rng=None, on_token=None):
    """First-person answer to the organism's own question."""
    answer = dedup_emerge(
        org,
        lambda: ThoughtArena(rng=rng).emerge(
            org,
            task="self_answer",
            question=question,
            fallback=lambda snap: narration.fallback_self_answer(snap, question),
            on_token=on_token,
            model=model,
            timeout=timeout,
        ),
    )
    if answer is None:
        answer = narration.fallback_self_answer(state_snapshot(org), question)
    return answer


# -- mud companion --------------------------------------------------------------


def mud_decide(org, user_message, model=None, timeout=None, rng=None, on_token=None):
    """One MUD move chosen by the organism itself; None on failure."""
    return ThoughtArena(rng=rng).emerge(
        org,
        task="mud",
        user_message=user_message,
        fallback=lambda _snap: None,
        on_token=on_token,
        model=model,
        timeout=timeout,
    )
