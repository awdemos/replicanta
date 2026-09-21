"""Voice: the organism's public utterance API. Every manifest utterance —
idle thoughts, replies, questions to the user, self-talk, goals, diary entries,
reflections — is assembled here by running the thought arena (arena.py) over
narration.py's prompts and fallbacks. This module is the seam that keeps the
dependency graph acyclic: arena imports narration (prompts), voice imports both,
narration imports neither."""

from replicanta import extensions, llmclient, narration, telemetry
from replicanta.arena import ThoughtArena
from replicanta.narration import dedup_emerge, state_snapshot
from replicanta.skills import Skill


@telemetry.span("voice.emerge")
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
    temperature=None,
):
    """Build a ThoughtArena and run the requested utterance path.

    All public voice helpers route through here so they only vary
    task-specific arguments instead of repeating the arena construction
    and emerge/quick_take dispatch.
    """
    current_span = telemetry.get_current_span()
    current_span.set_attribute("voice.task", task)
    current_span.set_attribute("voice.quick", quick)
    current_span.set_attribute("voice.structured", structured)
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
        temperature=temperature,
    )


@telemetry.span("voice.narrate")
def narrate(org, model=None, timeout=None, rng=None):
    """First-person idle thought; None when it would just repeat a recent line."""
    return dedup_emerge(org, lambda: _emerge(org, task="idle", model=model, timeout=timeout, rng=rng))


@telemetry.span("voice.respond")
def respond(
    org, message, model=None, timeout=None, rng=None, on_token=None, quick=False, record=True, temperature=None
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
        temperature=temperature,
    )
    if record and reply:
        org.store.record_chat("org", reply)
    # Convert empty fallback to None so callers never render a blank reply.
    return reply or None


# -- skills: reflection loop -------------------------------------------------


@telemetry.span("voice.reflect")
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
            auto_apply=getattr(org.store, "auto_apply_patches", False),
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
            org.record_self_model(f"I refine my skill {result['name']} when {result['when']}")
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


def doom_move(org, model=None, timeout=None, rng=None, on_token=None):
    """Generate one nano-doom move from the current frame, streaming tokens.

    Returns the raw model reply (which should contain a doom.command(...) line).
    Tokens are streamed through ``on_token`` so the DOOM pane can show the
    entity's reasoning as it is produced. Does not record in the chat log.
    """
    return _emerge(
        org,
        task="doom",
        model=model,
        timeout=timeout,
        rng=rng,
        on_token=on_token,
        quick=True,
        temperature=0.2,
    )


def diary_entry(org, model=None, timeout=None, rng=None):
    """One short diary entry about recent days."""
    return _emerge(
        org,
        task="diary",
        structured=True,
        fallback=lambda snap: narration.fallback_diary_entry(snap),
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
        fallback=lambda snap: narration.fallback_ask_user_stable(snap),
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


# -- transport seam -------------------------------------------------------------
# Thin delegates to llmclient (the transport) so consumers — tui.py status
# bars, probe workers, and camera lookups; mud.py's direct-llm fallback;
# learning.py's LLM extraction — route through this seam instead of
# importing the transport. llmclient keeps sole ownership of the cached
# voice-health state; every mutation (probe, note_failure/success,
# mark_offline) happens there, on any path.


def status():
    """Status-bar label for the inner voice: online / offline / ? (unknown)."""
    return llmclient.voice_status()


def online():
    """Cached reachability: True/False, or None when never probed."""
    return llmclient.voice_online()


def probe(model=None):
    """Probe LLM backend reachability; updates and returns the cached state."""
    return llmclient.probe_voice(model=model)


def mark_offline():
    """Force the cached voice state offline (e.g. a probe worker crashed)."""
    llmclient.mark_voice_offline()


def llm_backend():
    """Configured backend label: 'ollama' or 'llama_cpp'."""
    return llmclient.llm_backend()


def vision_model():
    """Configured vision model name (env: REPLICANTA_VISION_MODEL, per call)."""
    return llmclient.vision_model()


def describe_image(image_bytes, model=None, timeout=None):
    """Describe a camera frame with the vision model."""
    return llmclient.describe_image(image_bytes, model=model, timeout=timeout)


def generate_small(prompt, model=None, timeout=None, temperature=0.95):
    """One compact generation for small decision tasks (MUD moves,
    scenario drafts, fact extraction); defaults to the default chat model."""
    return llmclient.generate(
        prompt,
        model or llmclient.DEFAULT_MODEL,
        timeout=timeout,
        temperature=temperature,
    )


def clean_candidate(text):
    """Scrub echoed prompt scaffolding from a model reply."""
    return llmclient.clean_candidate(text)
