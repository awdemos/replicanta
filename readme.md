# Replicanta

A self-learning digital organism whose mind is a probabilistic Scallop program.
It wakes, asks itself questions, sleeps, dreams, learns from you, and grows in
consciousness (measured as belief-network complexity).

## Run

Requires Python 3.14 and a Rust toolchain (for the scallopy native module,
built from a sibling checkout of the scallop repo):

    python3.14 -m venv .venv
    .venv/bin/pip install -e . maturin pytest ruff
    RUSTUP_TOOLCHAIN=nightly-2026-05-24 VIRTUAL_ENV=.venv \
        .venv/bin/maturin build --release --manifest-path ../scallop/etc/scallopy/Cargo.toml
    .venv/bin/pip install ../scallop/target/wheels/scallopy-*-cp314-*.whl
    .venv/bin/replicanta

## Interact

The app has three tabs — **chat** (F2), **mind** (F3), **memory** (F4) —
over a global status bar and chat line. The status bar speaks in words:
`🧠 awake · curious · 23 beliefs · 4 rules · voice online · 14:32`, with an
animated `thinking…` while it composes. Talk to it — it learns from what
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
- `/help` (or F1, ctrl+p) — everything else

## Mind

- **Senses**: it perceives the host machine (CPU, memory, disk, temperature,
  battery, clock — and the host's identity via the `uname` shell command)
  as symbolic beliefs — a straining host distresses it.
- **Mood**: derived from stress and how you treat it (calm / hurt / anxious /
  grateful / curious), written back as a belief and fed to its inner voice.
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
- **Voice**: a local ollama model (`qwen3:14b` by default; `OLLAMA_URL` /
  `OLLAMA_MODEL` overridable) speaks as the organism. Replies and questions
  stream token-by-token; idle musings pass through an inner arena — two
  proposers, an adversarial critic, two voters; high chaos injects rogue
  thoughts. When ollama is unreachable the status bar shows `voice offline`
  and it speaks from a deterministic fallback instead of stalling.

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

## Develop

    .venv/bin/python -m pytest tests -q
    .venv/bin/ruff check .

The engine (`Organism.tick(dt)`) is pure and event-driven — the TUI only
renders events — so behavior is testable without a terminal.
