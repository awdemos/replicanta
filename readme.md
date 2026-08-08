# Scallop Organism

A self-learning digital organism whose mind is a probabilistic Scallop program.
It wakes, asks itself questions, sleeps, dreams, and grows in consciousness
(measured as belief-network complexity).

## Run

    make init-venv
    .env/bin/pip install maturin textual pytest
    RUSTUP_TOOLCHAIN=nightly-2026-05-24 VIRTUAL_ENV=.env .env/bin/maturin develop --release --manifest-path etc/scallopy/Cargo.toml
    .env/bin/python experiments/organism/tui.py

## Interact

- type anything: it answers from its beliefs
- `/chaos 0.8` — live randomness knob (0..1)
- `/focus color` — steer attention window; `/focus` to clear
- `/sleep`, `/wake` — force lifecycle transitions
- `/stats` — growth metrics; `/save` — persist

## Lifecycle

- **Wake**: self-questioning loop (chaos-governed), attention window narrows with fatigue.
- **Sleep**: recombination dreams at high chaos, wake-time validation, promotion.
- Growth = new beliefs, strengthened beliefs, committed rules, deeper derivations.

The organism's genome (`organism.scl`) is human-readable and evolves on disk;
`state.json` holds runtime state.
