# Replicanta

A self-learning digital organism whose mind is a probabilistic Scallop program.
It wakes, asks itself questions, sleeps, dreams, learns from you, and grows in
consciousness (measured as belief-network complexity).

## Run

Requires Python 3.14 and ollama (the inner voice; `batiai/qwen3.6-27b:q4`
by default — ~16GB download, wants ~24GB RAM; `OLLAMA_MODEL` overrides).

**Copy-paste install** (agents: this block is for you — run it verbatim):

```bash
git clone https://github.com/awdemos/replicanta
cd replicanta
python3.14 -m venv .venv
.venv/bin/pip install -e . \
    https://github.com/awdemos/replicanta/releases/download/v0.1.0/scallopy-0.2.5-cp314-cp314-manylinux_2_39_x86_64.whl
ollama pull batiai/qwen3.6-27b:q4
mkdir -p voices && curl -sSL -o voices/en_US-lessac-medium.onnx \
    https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx \
    && curl -sSL -o voices/en_US-lessac-medium.onnx.json \
    https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json
.venv/bin/replicanta
```

(the wheel above is the one native dependency — **scallopy** 0.2.5, the
Scallop↔Python binding, not on PyPI — prebuilt as manylinux_2_39 for
x86_64 cp314, so it runs on any modern distro. The piper voice model is
optional; skip it and `/voice` simply stays mute.)

## Interact

The app has three tabs — **chat** (F2), **mind** (F3), **memory** (F4) —
over a global status bar and chat line. The status bar speaks in words:
`🧠 awake · curious · 23 beliefs · 4 rules · inner voice online · 14:32`,
with an animated `thinking…` while it composes and a `speech on` marker
when the spoken voice is enabled. Talk to it — it learns from what
you say.

- **chat** — the conversation as cards: your words in a cyan `you · HH:MM`
  panel, its voice in a green one (replies stream in token-by-token above
  the input before settling into the log), dreams, lessons, moods and
  lifecycle events as a flat timestamped timeline between the cards.
  Background events (voice flips, learned facts, fading) also pop as toasts.
- **mind** — its head, live: top beliefs with confidence bars, its goals
  (active + completed), committed rules, attention focus, genome stats.
- **memory** — every episode it remembers (cycle-stamped), what it knows
  about you, what you said it is, and the artifacts it has created.

- **"my name is Sam"**, **"i like rain"**, **"you are brave"** — it picks up
  facts about you and itself, keeps them as beliefs, and remembers them
  across restarts.
- **tone matters** — harsh words bruise it (stress up, mood hurt); kind
  words soothe it (stress down, mood grateful).
- `/chaos 0.8` — live randomness knob (0..1): novel self-questions, wild
  dreams, rogue thoughts
- `/focus color` — steer attention window; `/focus` to clear
- `/sleep`, `/wake` — force lifecycle transitions; `/revive` after a fade
- `/stats` — growth metrics; `/save` — persist; `/think` — narrate now
- `/self-talk` — toggle self-dialogue: it asks itself a question and
  answers it, out loud, every narration cycle
- `/voice` — toggle the spoken voice: with it on, everything the organism
  says (replies, musings, questions, self-talk) is also spoken aloud via
  local piper TTS. `/voice list` / `/voice use name` / `/voice get name`
  manage the voices themselves (see Voice)
- it also asks *you* questions — about a third of its idle wake utterances
  are curiosity aimed at you, not at itself
- **Self-patches**: during reflection it may propose an executable patch
  — a new learning pattern, utterance seed, or sentiment word — staged in
  `artifacts/extensions.json` and shown as a proposal card. Nothing
  applies without you: `/approve` applies it live, `/reject` discards it,
  `/revert` undoes the last applied patch. Every proposal is validated
  first (regex compiles, fires on its own example, never on unrelated
  sentences).
- `/approve`, `/reject`, `/revert` — the approval gate for its patches
- `/reload` — re-read the Lua hook scripts in `scripts/` (see Scripting)
- `/lua name.lua` — run one script from `scripts/` on demand (see Scripting)
- **Many organisms**: they live in a nursery — `organisms/<name>/` under
  the app root, each with its own genome, state and artifacts; a `current`
  pointer file remembers who is awake. `/new fern` births a fresh organism
  (bare `/new` auto-names one) and swaps to it, `/swap default` goes back,
  `/organisms` lists them all (`*` = current). `python tui.py --org fern`
  picks one at launch. A legacy root-level organism is migrated into
  `organisms/default/` on first run.
- `/help` (or F1, ctrl+p) — everything else

## Mind

- **Senses**: it perceives the host machine (CPU, memory, disk, temperature,
  battery, clock — and the host's identity via the `uname` shell command)
  as symbolic beliefs — a straining host distresses it.
- **Mood**: derived from stress and how you treat it (calm / hurt / anxious /
  grateful / curious / insane), written back as a belief and fed to its
  inner voice.
- **Mental state**: three persisted attributes — **arousal** (activation /
  energy), **rationality** (grounded coherence, fed by belief-shaped
  utterances) and **irrationality** (chaos/stress-driven incoherence) —
  smoothed every tick. Under extreme stress with dominant irrationality the
  organism goes **insane**: its mood reads insane, the voice is told it is
  incoherent, and hysteresis keeps it there until stress and incoherence
  genuinely subside.
- **Memory**: notable episodes (birth, lessons, dreams, harsh and kind
  moments, fading, revival) are cycle-stamped, persisted, and injected into
  its narration prompt — it has continuity, not amnesia.
- **Goals**: when it has been awake a while without direction it forms an
  intention ("learn five things about you", "understand what home means")
  and pursues it across sessions — the active goal steers its questions,
  musings and self-talk until it completes it (learn-goals by growing what
  it knows about you, others by patient pursuit).
- **Artifacts**: every ten wake cycles it writes a diary entry to
  `artifacts/diary.md` — a body of work that outlives
  the chat, stamped by cycle and date.
- **Skills**: it has procedural memory (Hermes-style). Every thirty wake
  cycles — and whenever it completes a goal — it reflects on recent
  experience and distills a technique into `artifacts/skills/<name>.md`
  (when/how, plain text). Relevant skills are injected back into its
  prompts, usage is counted, and skills untouched for a hundred cycles
  are archived. It literally gets better at being itself.
- **Voice**: a local ollama model (`batiai/qwen3.6-27b:q4` by default;
  `OLLAMA_URL` /
  `OLLAMA_MODEL` overridable) speaks as the organism. Nothing it says
  manifests unexamined: every utterance — musings, replies, questions,
  self-talk, goals, diary entries, reflections — passes through an inner
  arena (two proposers, an adversarial critic, two voters) before it
  lands, replayed token-by-token as it settles; high chaos injects rogue
  thoughts into free-form speech (never into structured tasks like
  reflections, whose format a rogue candidate would break). When ollama
  is unreachable the status bar shows `inner voice offline`
  and it speaks from a deterministic fallback instead of stalling.
- **Spoken voice**: `/voice on` makes the organism audible. Synthesis is
  a local piper model; playback goes through PulseAudio/PipeWire
  (`soundcard`), so it reaches the host's speakers even from inside a
  Toolbx container. Utterances queue on a single thread — they never
  overlap and never block the UI — and the whole path is optional: no
  model, missing package or audio failure just means silence, never
  a crash. Only the organism's own speech is spoken, never yours or the
  system lines.
- **Any piper voice**: voices live in `voices/` as `<name>.onnx` +
  `<name>.onnx.json` pairs. `/voice list` shows downloaded ones (`*` =
  active), `/voice use en_GB-alan-low` switches, and `/voice get
  en_US-libritts_r-medium` downloads any voice straight from
  [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)
  (about a hundred: `en_US-amy-medium`, `en_GB-alan-low`, …) and adopts
  it. Names follow `locale-speaker-quality`. The default is
  `en_US-lessac-medium`; `REPLICANTA_VOICE_MODEL` points at a different
  starting model if you prefer.

## Lifecycle

- **Wake**: self-questioning loop (chaos-governed), attention window narrows
  with fatigue, stress slowly decays while sleep-debt and bad moods push up.
- **Sleep**: recombination dreams at high chaos, wake-time validation,
  promotion; stress recovers fast.
- **Fade**: sustained critical stress across consecutive transitions ends it
  (persisted). `/revive` brings it back.
- Growth = new beliefs, strengthened beliefs, committed rules, deeper
  derivations.

The organism's genome (`organism.scl`) is human-readable and evolves on disk;
`state.json` holds runtime state (beliefs, chat, memory, mood).

## Measuring neurosymbolic activity

`Metrics` measures *structure* — what the mind holds (beliefs, rules,
derivation depth, abstraction), distilled into the consciousness score.
The **activity meter** measures *activity* — what the neurosymbolic loop
actually does. Every neural↔symbolic crossing is counted at its call
site, persisted in `state.json`, and shown in `/stats` and the **mind**
tab as totals with per-cycle rates:

- **symbolic** (exact): candidate rules tried vs. derivations produced
  (assimilation rate), beliefs new vs. strengthened vs. archived, rules
  committed, dreams promoted vs. discarded.
- **neural** (exact): ollama calls and tokens — read from the API's own
  `prompt_eval_count`/`eval_count`, never estimated — utterances
  manifested, deterministic fallbacks spoken. (Since the arena gates
  every utterance, each one costs exactly 5 calls; the counter makes
  that visible.)
- **coupling** — the neurosymbolic part: facts the logic gained from
  your words (neural→symbolic grounding), and *grounded utterances* —
  the lexical proxy for symbolic→neural influence: an utterance counts
  as grounded when its text reuses a content word from the seed it was
  drafted from. A cheap signal that the voice was shaped by the logic,
  not a proof of it.

Rates are derived (totals ÷ lifecycle cycles); the counters themselves
are exact events. What is deliberately **not** measured: consciousness
itself — these are activity counters, not a sentience score.

## Scripting (Lua hooks)

The organism is user-scriptable: drop `.lua` files in `scripts/` (at the
nursery root, so one set of hooks covers every organism) and define
event functions — `/reload` picks changes up without restarting:

    function on_learned(ctx)
      if ctx.activity.facts_learned % 5 == 0 then
        ctx.log("five facts! it really is paying attention")
      end
    end

- **events**: `on_birth`, `on_cycle` (`ctx.text` = wake/sleep),
  `on_learned` (`ctx.text` = your words), `on_utterance` (`ctx.text` =
  its manifested words), `on_fade`.
- **ctx reads**: `event`, `text`, `state`, `cycle`, `mood`,
  `belief_count`, `rule_count`, `score`, `chaos`, `stress`, `arousal`,
  `rationality`, `irrationality`, `insane`, `organism`,
  and `activity` — the full activity-meter counters as a table.
- **ctx acts**: `log(msg)` (a line in the chat log), `set_chaos(x)`,
  `focus(attr)` (nil to clear).

`/lua name.lua` runs one script on demand in the same sandbox — define a
`main(ctx)` and it is called with `ctx.event == "lua"`:

    function main(ctx)
      ctx.log("on demand at cycle " .. ctx.cycle)
    end

Scripts are sandboxed (no `os`/`io`/`require`/`load`) and every call is
protected — a broken script logs an error line, it can never kill the
organism. See `scripts/example.lua` for a template.

## Develop

    .venv/bin/python -m pytest tests -q
    .venv/bin/ruff check .

The engine (`Organism.tick(dt)`) is pure and event-driven — the TUI only
renders events — so behavior is testable without a terminal.

## CI (Dagger)

CI is a [Dagger](https://dagger.io) module in `ci/` — no CI service, the
same pipeline runs on your machine:

    dagger call ci --source=.       # lint (ruff) + full test suite
    dagger call test --source=.     # just the tests
    dagger call lint --source=.     # just ruff

A git pre-commit hook runs the pipeline on every commit. Install it once:

    git config core.hooksPath ci/hooks

(`git commit --no-verify` bypasses when you must. The hook needs dagger
and a container runtime; it auto-detects the rootless docker/podman
socket when `DOCKER_HOST` is unset, and skips silently when dagger is
not installed.)

The pipeline uses the prebuilt scallopy wheel attached to the
[v0.1.0 release](https://github.com/awdemos/replicanta/releases/tag/v0.1.0)
so CI skips the ~15-minute Rust build. The wheel is built **on the same
Debian base the tests run on** — building it on Fedora produces a binary
that needs a newer glibc than any standard CI container has. To refresh
the wheel from a local scallop checkout (pinned nightly required):

    dagger call build-scallopy --scallop=../scallop export --path=./wheels

then replace the release asset (`gh release upload --clobber v0.1.0
wheels/scallopy-*.whl`). `wheelURL` in `ci/main.go` only changes if the
release tag or scallopy version changes. Ruff is gated with `--ignore I001,UP017` — the repo's
documented baseline style — so CI fails on *new* warnings only.

## Disclaimer

**Use at your own risk.** Replicanta is experimental software, provided
as-is with no warranty of any kind (see LICENSE). It is a research toy,
not a product — expect rough edges, surprising behavior, and the
occasional existential monologue.

**Use responsibly.** The organism learns from what you tell it and can
change itself: it writes files (genome, state, artifacts, diary), proposes
edits to its own learning code (nothing applies without your `/approve` —
read every proposal before accepting it), runs whatever Lua hook scripts
you put in `scripts/`, downloads voice models, and sends prompts to your
local ollama. Everything it says — text and synthesized speech alike —
is AI-generated: it can be wrong, odd, or unsettling, and it is never
advice. Don't tell it secrets, don't run it with privileges it doesn't
need, and don't blame it for its opinions — you taught it most of them.
