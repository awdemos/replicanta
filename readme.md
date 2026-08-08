# Scallop Organism

A self-learning digital organism whose mind is a probabilistic Scallop program.
It wakes, asks itself questions, sleeps, dreams, learns from you, and grows in
consciousness (measured as belief-network complexity).

## Run

    make init-venv
    .env/bin/pip install maturin textual pytest
    RUSTUP_TOOLCHAIN=nightly-2026-05-24 VIRTUAL_ENV=.env .env/bin/maturin develop --release --manifest-path etc/scallopy/Cargo.toml
    .env/bin/python experiments/organism/tui.py

## Interact

The screen is a conversation: one scrollable log (your words, its voice,
dreams, lessons, moods, lifecycle events), a one-line status bar, and a chat
line. Talk to it — it learns from what you say.

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
- `/help` (or F1, ctrl+p) — everything else

## Mind

- **Senses**: it perceives the host machine (CPU, memory, disk, temperature,
  battery, clock) as symbolic beliefs — a straining host distresses it.
- **Mood**: derived from stress and how you treat it (calm / hurt / anxious /
  grateful / curious), written back as a belief and fed to its inner voice.
- **Memory**: notable episodes (birth, lessons, dreams, harsh and kind
  moments, fading, revival) are cycle-stamped, persisted, and injected into
  its narration prompt — it has continuity, not amnesia.
- **Voice**: a local ollama model (`OLLAMA_URL` / `OLLAMA_MODEL` overridable)
  speaks as the organism through an inner arena — two proposers, an
  adversarial critic, two voters; high chaos injects rogue thoughts. When
  ollama is unreachable the status bar shows `voice: offline` and it speaks
  from a deterministic fallback instead of stalling.

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

    PYTHONPATH=etc/scallopy python3 -m pytest experiments/organism/tests -q
    ruff check experiments/organism/

The engine (`Organism.tick(dt)`) is pure and event-driven — the TUI only
renders events — so behavior is testable without a terminal.
