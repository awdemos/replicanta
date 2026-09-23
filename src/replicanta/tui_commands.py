"""Pure UI helpers for the organism TUI and web front-ends: slash-command
metadata, the dispatch registry, tab completion, help text, and the shared
/voice and /auto-apply command behaviors each frontend renders for itself.
No textual imports — unit testable without a terminal. Sentiment scorers
live in sentiment.py."""

from pathlib import Path

from replicanta import activity, extensions, fileutil, nursery, speech
from replicanta.tui_views import STYLE_DIM, STYLE_LEARNED, STYLE_WARN

COMMANDS = [
    # State
    ("/chaos", "/chaos 0..1", "set randomness 0-1", "State"),
    ("/focus", "/focus attr", "lock attention on attr (bare /focus clears)", "State"),
    ("/sleep", "/sleep", "force wake->sleep", "State"),
    ("/wake", "/wake", "force sleep->wake", "State"),
    ("/soothe", "/soothe", "comfort the organism: relieve its stress", "State"),
    ("/revive", "/revive", "bring a faded organism back", "State"),
    ("/stats", "/stats", "show growth metrics", "State"),
    ("/think", "/think", "narrate thoughts now", "State"),
    ("/self-talk", "/self-talk", "let the organism speak to itself", "State"),
    (
        "/actuation",
        "/actuation",
        "toggle entities acting on their own utterances",
        "State",
    ),
    (
        "/persona",
        "/persona [name|off|list]",
        "activate, clear, or list personas",
        "State",
    ),
    (
        "/auto-apply",
        "/auto-apply [on|off]",
        "toggle automatic self-patch application",
        "State",
    ),
    (
        "/visualize",
        "/visualize [beliefs|attributes|activity|memories|recent|mood|sentiment|stress|summary]",
        "render an RDD chart of organism state",
        "State",
    ),
    (
        "/hand",
        "/hand [state|posture name [dur]|actuator f j side act [dur]|goal kind [dur]|volition [on|off]|emotion s a mood]",
        "drive the tendon-hand effector; with no args show state",
        "State",
    ),
    (
        "/brain",
        "/brain [status|run|train|optimize <task>|adapt|bank|info|last|help]",
        "evolve or inspect the fly-brain reservoir (rsi-wetware-rs)",
        "State",
    ),
    (
        "/doom",
        "/doom [start [skill]|stop|status|frame|help|w/a/s/d/shoot/q/e]",
        "play DOOM (doom-ascii) in a full-screen overlay",
        "State",
    ),
    # Voice
    (
        "/voice",
        "/voice [on|off|list|use|get]",
        "spoken voice: toggle, list, switch, download piper voices",
        "Voice",
    ),
    # Senses
    (
        "/listen",
        "/listen",
        "push-to-talk: start/stop the mic, speak to it (F5)",
        "Senses",
    ),
    (
        "/microphone",
        "/microphone [list|use dev]",
        "mic status, list input devices, choose one",
        "Senses",
    ),
    ("/look", "/look", "grab a camera frame and see it (F6)", "Senses"),
    (
        "/camera",
        "/camera [list|use dev]",
        "camera status, list devices, choose one",
        "Senses",
    ),
    # MUD
    (
        "/mud",
        "/mud [map|story|quest|pause|resume|step|reset|scenario d…]",
        "toggle or control the dungeon crawl (text adventure)",
        "MUD",
    ),
    # Organisms
    ("/new", "/new [name]", "birth a new organism and swap to it", "Organisms"),
    ("/swap", "/swap name", "swap to another organism", "Organisms"),
    ("/organisms", "/organisms", "list all organisms", "Organisms"),
    (
        "/group",
        "/group start a b | stop",
        "group chat: organisms talk with you and each other",
        "Organisms",
    ),
    # System
    (
        "/export",
        "/export [name]",
        "save chat log to ~/.replicanta/exports/",
        "System",
    ),
    ("/save", "/save", "persist state + genome", "System"),
    (
        "/modules",
        "/modules [manage]",
        "open module manager (or list via /modules)",
        "System",
    ),
    (
        "/approve",
        "/approve",
        "apply the organism's pending genome patch (manual mode)",
        "System",
    ),
    ("/reject", "/reject", "discard the pending genome patch (manual mode)", "System"),
    ("/revert", "/revert", "undo the last applied genome patch", "System"),
    ("/reload", "/reload", "re-read the lua hook scripts", "System"),
    ("/lua", "/lua name.lua", "run a lua script from scripts/ on demand", "System"),
    ("/git", "/git [on|off|status]", "toggle or show git sensing", "System"),
    ("/quit", "/quit", "save and quit (same as F10)", "System"),
    # Help
    ("/help", "/help", "this help screen", "Help"),
]

COMMAND_NAMES = [c[0] for c in COMMANDS]


def palette_items():
    """Return all slash commands as (name, usage, description, category)."""
    return COMMANDS


def filter_commands(query):
    """Return commands whose name, usage, or description matches query."""
    q = query.lower().strip()
    if not q:
        return COMMANDS
    return [c for c in COMMANDS if any(q in part.lower() for part in c[:3])]


# -- dispatch registry --------------------------------------------------------
#
# COMMAND_HANDLERS maps every COMMANDS name to its handler so tui.py's
# _dispatch is a plain dict lookup. Handlers take (app, parts) where parts
# is the split command line, and keep the exact behavior of the former
# if/elif branches — validation, store mutation, and log styling included.
# Module-owned verbs (/visualize /hand /brain /doom) delegate to the app's
# thin dispatch wrappers (or its doom controller), which route through the
# module CommandService.


def _cmd_chaos(app, parts):
    if len(parts) != 2:
        app._append_log(
            f"/chaos needs a number 0-1 (now {app.org.store.chaos:.2f})",
            STYLE_DIM,
        )
        return
    value = float(parts[1])
    if not 0.0 <= value <= 1.0:
        raise ValueError("chaos must be between 0 and 1")
    app.org.store.chaos = value
    app._append_log(f"chaos: {value:.2f}", STYLE_DIM)
    app.refresh_status()


def _cmd_focus(app, parts):
    if len(parts) == 2:
        app.org.window.focus(parts[1])
        app.org.store.attention = app.org.window.pairs
        app._append_log(f"attention locked on {parts[1]}", STYLE_DIM)
    else:
        app.org.window.focus(None)
        app._append_log("attention floating free", STYLE_DIM)


def _cmd_sleep(app, parts):
    for event in app.org.force_state("sleep"):
        app._render_event(event)


def _cmd_wake(app, parts):
    for event in app.org.force_state("wake"):
        app._render_event(event)


def _cmd_soothe(app, parts):
    relief = app.org.soothe()
    if relief > 0.0:
        app._append_log(f"you soothe it — stress eases by {relief:.2f}", STYLE_DIM)
    else:
        app._append_log("the organism is already at ease.", STYLE_DIM)
    app.refresh_status()


def _cmd_revive(app, parts):
    if app.org.revive():
        app._append_log("revived: the organism stirs back into existence.", STYLE_DIM)
        app._maybe_narrate()
    else:
        app._append_log(
            f"/revive: it is not faded (state {app.org.lifecycle.state}).",
            STYLE_DIM,
        )


def _cmd_stats(app, parts):
    for line in activity.stats_report(app.org):
        app._append_log(line, STYLE_DIM)


def _cmd_save(app, parts):
    app.action_save_now()


def _cmd_export(app, parts):
    try:
        dest = app._export_chat(parts[1] if len(parts) > 1 else None)
        app._append_log(f"— chat exported to {dest} —", STYLE_DIM, stamp=True)
    except fileutil.UnsafePathError:
        app._append_log(
            "— export failed: give a plain filename (exports land in ~/.replicanta/exports/) —",
            STYLE_WARN,
            stamp=True,
        )
    except OSError as exc:
        app._append_log(f"— export failed: {exc} —", STYLE_WARN, stamp=True)


def _cmd_think(app, parts):
    app.action_think_now()


def _cmd_listen(app, parts):
    app._toggle_listen()


def _cmd_microphone(app, parts):
    app._microphone(parts[1:])


def _cmd_look(app, parts):
    app._look_now()


def _cmd_camera(app, parts):
    app._camera(parts[1:])


def _cmd_mud(app, parts):
    app._mud.command(parts[1:])


def _cmd_reload(app, parts):
    app.org.hooks.reload()
    count = len(app.org.hooks.scripts)
    app._append_log(
        f"lua hooks reloaded ({count} script{'s' if count != 1 else ''})",
        STYLE_DIM,
    )


def _cmd_lua(app, parts):
    if len(parts) != 2:
        names = ", ".join(s.name for s in app.org.hooks.scripts)
        app._append_log(f"/lua needs a script name (scripts/: {names or 'none'})", STYLE_DIM)
        return
    app._append_log(app.org.hooks.run(parts[1], app.org), STYLE_DIM)


def _cmd_organisms(app, parts):
    names = nursery.list_organisms(app.root)
    current = app.org.dir_path.name
    listing = ", ".join(f"*{n}" if n == current else n for n in names) or "(none)"
    app._append_log(f"organisms: {listing}  (* = current)", STYLE_DIM)


def _cmd_group(app, parts):
    app._group_command(parts[1:])


def _cmd_new(app, parts):
    new_name = parts[1] if len(parts) == 2 else nursery.next_name(app.root)
    try:
        nursery.create(app.root, new_name, Path(app.root) / "organism.scl")
    except (ValueError, OSError) as exc:
        app._append_log(f"/new: {exc}", STYLE_WARN)
    else:
        app._swap_to(new_name)


def _cmd_swap(app, parts):
    if len(parts) != 2:
        app._append_log("/swap needs a name — /organisms to list.", STYLE_DIM)
        return
    if parts[1] not in nursery.list_organisms(app.root):
        names = ", ".join(nursery.list_organisms(app.root)) or "(none)"
        app._append_log(f"/swap: no organism {parts[1]!r} — have: {names}", STYLE_WARN)
        return
    app._swap_to(parts[1])


def voice_command(args):
    """Shared /voice behavior for the TUI and web frontends: performs the
    speech-state transition and returns (message, warn) for the frontend to
    render. Returns None for ``get`` — each frontend runs the download in
    its own transport (the TUI in a background worker with progress, the
    web UI inline in the request)."""
    if not args or args[0] in ("on", "off"):
        if args:
            speech.set_enabled(args[0] == "on")
        else:
            speech.set_enabled(not speech.enabled)
        state = "on" if speech.enabled else "off"
        if speech.enabled and not speech.ready():
            if not speech.available():
                message = (
                    f"spoken voice {state}, but no piper model at "
                    f"{speech.model_path()} — staying mute "
                    f"(/voice get en_US-lessac-medium)"
                )
            else:
                message = (
                    "spoken voice on, but the piper/soundcard packages are "
                    "missing — staying mute; install the 'voice' extra "
                    "(uv pip install -e '.[voice]')"
                )
            return (message, True)
        if speech.enabled:
            speech.say("I can speak now.")
            return ("spoken voice on — the organism speaks aloud (piper tts)", False)
        return ("spoken voice off", False)
    if args[0] == "list":
        voices = speech.list_voices()
        active = speech.voice_name()
        listing = ", ".join(f"*{v}" if v == active else v for v in voices) or "(none — /voice get en_US-lessac-medium)"
        return (f"voices: {listing}  (* = active)", False)
    if args[0] == "use" and len(args) == 2:
        if speech.set_voice(args[1]):
            speech.say("This is my new voice.")
            return (f"voice: {speech.voice_name()}", False)
        have = ", ".join(speech.list_voices()) or "(none)"
        message = f"/voice use: no voice {args[1]!r} — have: {have}. /voice get {args[1]} downloads it"
        return (message, True)
    if args[0] == "get" and len(args) == 2:
        return None
    return ("/voice [on|off] · /voice list · /voice use name · /voice get name", False)


def auto_apply_command(store, args):
    """Shared /auto-apply behavior for the TUI and web frontends: apply the
    on/off transition to the store and return the status line."""
    if args and args[0] in ("on", "off"):
        store.auto_apply_patches = args[0] == "on"
        store.dirty = True
        state = "on" if store.auto_apply_patches else "off"
        return f"auto-apply patches: {state}"
    state = "on" if store.auto_apply_patches else "off"
    return f"auto-apply patches is {state} — use /auto-apply on|off"


def _cmd_voice(app, parts):
    args = parts[1:]
    result = voice_command(args)
    if result is None:
        app._voice_download(args[1])
        return
    text, warn = result
    app._append_log(text, STYLE_WARN if warn else STYLE_DIM)
    if not args or args[0] in ("on", "off"):
        app.refresh_status()


def _cmd_self_talk(app, parts):
    app._self_talk_on = not app._self_talk_on
    if app._self_talk_on:
        app._append_log("self-talk on — the organism may speak to itself.", STYLE_DIM)
        if app.org.lifecycle.state == "wake":
            app._maybe_self_talk()
    else:
        app._append_log("self-talk off", STYLE_DIM)


def _cmd_actuation(app, parts):
    """Flip the persisted entity-actuation flag. getattr-defaults to True so
    the toggle keeps working even if the organism schema predates the flag;
    plain assignment persists it once the schema knows the field."""
    enabled = not getattr(app.org, "entity_actuation", True)
    app.org.entity_actuation = enabled
    if enabled:
        app._append_log(
            "entity actuation ON — entities may move/game from their own utterances",
            STYLE_DIM,
        )
    else:
        app._append_log("entity actuation OFF — entities only act on direct commands", STYLE_DIM)


def _cmd_approve(app, parts):
    entry = extensions.approve(app.org.dir_path / "artifacts" / "extensions.json")
    if entry:
        app.org.store.remember("skill", f"patch applied ({entry['kind']})")
        app._append_log(
            f"patch applied ({entry['kind']}) — live now, no restart needed",
            STYLE_LEARNED,
            stamp=True,
        )
    else:
        app._append_log("/approve: no pending patch.", STYLE_DIM)


def _cmd_reject(app, parts):
    entry = extensions.reject(app.org.dir_path / "artifacts" / "extensions.json")
    if entry:
        app.org.store.remember("skill", f"patch rejected ({entry['kind']})")
        app._append_log(f"patch rejected ({entry['kind']})", STYLE_DIM, stamp=True)
    else:
        app._append_log("/reject: no pending patch.", STYLE_DIM)


def _cmd_auto_apply(app, parts):
    app._append_log(auto_apply_command(app.org.store, parts[1:]), STYLE_DIM)


def _cmd_revert(app, parts):
    entry = extensions.revert_last(app.org.dir_path / "artifacts" / "extensions.json")
    if entry:
        app.org.store.remember("skill", f"patch reverted ({entry['kind']})")
        app._append_log(f"patch reverted ({entry['kind']})", STYLE_LEARNED, stamp=True)
    else:
        app._append_log("/revert: no applied patches yet.", STYLE_DIM)


def _cmd_quit(app, parts):
    app.action_quit()


def _cmd_help(app, parts):
    app.action_help()


def _cmd_git(app, parts):
    app._git_command(parts[1:])


def _cmd_persona(app, parts):
    app._persona_command(parts[1:])


def _cmd_modules(app, parts):
    app._modules_command(parts[1:])


def _cmd_visualize(app, parts):
    app._visualize_command(parts[1:])


def _cmd_hand(app, parts):
    app._hand_command(parts[1:])


def _cmd_brain(app, parts):
    app._brain_command(parts[1:])


def _cmd_doom(app, parts):
    app._doom.command(parts[1:])


COMMAND_HANDLERS = {
    "/chaos": _cmd_chaos,
    "/focus": _cmd_focus,
    "/sleep": _cmd_sleep,
    "/wake": _cmd_wake,
    "/soothe": _cmd_soothe,
    "/revive": _cmd_revive,
    "/stats": _cmd_stats,
    "/think": _cmd_think,
    "/self-talk": _cmd_self_talk,
    "/actuation": _cmd_actuation,
    "/persona": _cmd_persona,
    "/auto-apply": _cmd_auto_apply,
    "/visualize": _cmd_visualize,
    "/hand": _cmd_hand,
    "/brain": _cmd_brain,
    "/doom": _cmd_doom,
    "/voice": _cmd_voice,
    "/listen": _cmd_listen,
    "/microphone": _cmd_microphone,
    "/look": _cmd_look,
    "/camera": _cmd_camera,
    "/mud": _cmd_mud,
    "/new": _cmd_new,
    "/swap": _cmd_swap,
    "/organisms": _cmd_organisms,
    "/group": _cmd_group,
    "/export": _cmd_export,
    "/save": _cmd_save,
    "/modules": _cmd_modules,
    "/approve": _cmd_approve,
    "/reject": _cmd_reject,
    "/revert": _cmd_revert,
    "/reload": _cmd_reload,
    "/lua": _cmd_lua,
    "/git": _cmd_git,
    "/quit": _cmd_quit,
    "/help": _cmd_help,
}


CHAT_HISTORY_LIMIT = 50


def completion_matches(value):
    """Every command name completing value's first token, or None when
    the value isn't a completable slash command."""
    token = value.split()[0] if value.strip() else ""
    if not token.startswith("/"):
        return None
    return [n for n in COMMAND_NAMES if n.startswith(token)]


def complete_command(value, matches, index):
    """Tab-cycle slash completion over a fixed candidate list.

    `matches` are the candidates captured when the typed token last
    changed (see completion_matches); `index` is the previously used
    match index (0 = first match). Returns (completed_value, next_index).
    Anything typed after the first word of `value` is preserved."""
    if not matches:
        return value, 0
    used = index % len(matches)
    first = value.split()[0] if value.strip() else ""
    return matches[used] + value[len(first) :], (used + 1) % len(matches)


def history_push(history, text):
    """Remember a submitted chat line, deduped against the previous line."""
    text = text.strip()
    if not text or (history and history[-1] == text):
        return history
    history.append(text)
    if len(history) > CHAT_HISTORY_LIMIT:
        del history[: len(history) - CHAT_HISTORY_LIMIT]
    return history


def history_browse(history, index, draft, current, delta):
    """Move through chat history. `index` is the browse position (-1 = not
    browsing), `draft` the input value saved when browsing started, `current`
    the live input value, `delta` -1 = older (up) / +1 = newer (down).
    Returns (new_index, draft, value) where `value` is the text to show or
    None when the input should stay untouched."""
    n = len(history)
    if n == 0:
        return index, draft, None
    if index == -1:
        if delta > 0:
            return index, draft, None
        draft = current
        index = n - 1
    else:
        target = index + delta
        if target < 0:
            return index, draft, None
        if target >= n:
            return -1, draft, draft
        index = target
    return index, draft, history[index]


def help_text():
    lines = ["REPLICANTA — type / in the chat line; tab completes.", ""]
    for _name, usage, desc, _category in COMMANDS:
        if len(usage) <= 24:
            lines.append(f"{usage:<26} {desc}")
        else:
            # long signatures (/visualize, /hand, /doom): signature on its
            # own line, description indented beneath the command column
            lines.append(f"  {usage}")
            lines.append(f"{'':<26} {desc}")
    lines += [
        "",
        "keyboard",
        "ctrl+p  command palette",
        "F1      this help",
        "F3      being overlay: mind · memory · inner · cells",
        "ctrl+b  toggle the sidebar",
        "F5       push-to-talk (/listen)",
        "F6       look through the camera (/look)",
        "F9       module manager: enable/disable Lua modules",
        "F10      quit (now asks for confirmation)",
        "ctrl+q   quit immediately",
        "ctrl+c   press twice to quit",
        "ctrl+s   save now",
        "ctrl+t   think now",
        "ctrl+m   toggle terminal mouse capture (on by default; off = text selectable)",
        "ctrl+shift+c copy the chat log",
        "tab      complete a slash command",
        "up/down  recall previous chat lines (drive DOOM on its overlay)",
        "mouse    clicks on by default; ctrl+m toggles native text selection",
        "",
        "modules: /modules opens the manager. enable fly-brain, save, then use",
        "/brain status | run <task> | train <task> | optimize <task> | adapt | bank | info | last | help.",
        "/brain run digits runs the real larval-Drosophila connectome reservoir.",
        "",
        "fly brain provenance: the harness is the rsi-wetware-rs CLI",
        "(https://github.com/awdemos/rsi-wetware-rs), a Rust implementation of",
        "an L1–L5 recursive-self-improvement loop over the complete larval fruit-fly",
        "connectome (Winding et al., Science 2023). Replicanta only spawns/parses it.",
        "",
        "voice: /voice on|off toggles piper TTS. /voice list shows installed",
        "voices, /voice use <name> switches, /voice get <name> downloads one.",
        "If enabled but no model is present the organism stays mute.",
        "",
        "mud: /mud toggles; while it runs, type moves directly",
        "(go north, take torch, look, inventory) or prose as a hint.",
        "/mud map|story|quest show the world; /mud pause|resume|step",
        "control auto-turns; /mud scenario <description> dreams up a new",
        "adventure; /mud reset restarts the current one.",
        "",
        "doom-ascii: /doom start [skill] launches DOOM in a full-screen",
        "overlay (esc closes it; the game keeps running). While playing,",
        "/doom w/a/s/d moves, q/e strafes, shoot fires, use opens doors,",
        "and 1-7 switches weapon. /doom stop ends it.",
        "",
        "group: /group start fern willow (or 'all', or a nursery group",
        "name) opens a shared chat; everything you type is broadcast to",
        "every member, and each one answers in turn. Address a single",
        "member with 'fern: …' or",
        "'@fern …'. /group stop ends it (members keep their memories).",
        "",
        "scripting: drop .lua files in scripts/ (see scripts/example.lua);",
        "they get on_birth/on_cycle/on_learned/on_utterance/on_fade(ctx)",
        "called at those moments — /reload re-reads them, /lua name.lua",
        "runs one's main(ctx) on demand.",
        "",
        "git sensing: /git on|off toggles whether the organism feels the",
        "worktree state; /git status shows the current repo summary.",
    ]
    return "\n".join(lines)
