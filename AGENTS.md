# Agent Guidelines for Replicanta

Replicanta is a local, neurosymbolic AI companion written in Python. It couples a Scallop symbolic reasoner with a local LLM backend, and provides a TUI, web interface, voice/vision, and self-modifying agent hooks.

## Project Layout

```
src/replicanta/    # Core package (organism, memory, hooks, TUI, etc.)
modules/           # Persona modules (software-engineer, creative-writer, socratic-philosopher, visual-state)
scripts/           # Lua hook examples
tests/             # pytest suite
ci/                # Dagger CI module (Go)
docs/              # Assets and superpower plans
replicanta.toml    # Runtime persona config
pyproject.toml     # Project metadata and ruff/pytest config
requirements-ci.txt# Hash-pinned CI dependencies exported from uv.lock
```

## Critical Rules

1. **Dagger CI is the source of truth.** Before committing, run `dagger call ci --source=. --progress=plain` (or the `ci/hooks/pre-commit` hook) to run lint + tests.
2. **Python 3.14 only.** The Scallopy wheel is pinned to CPython 3.14; do not change `requires-python` without rebuilding the wheel.
3. **Scallopy is not on PyPI.** Install the pinned wheel from the v0.1.0 release or place a matching wheel under `wheels/`.
4. **Self-modification is gated.** Auto-apply of code patches is off by default; preserve that default unless the change explicitly addresses agent safety.
5. **Lua hooks run sandboxed.** New hooks must respect the Lua sandbox in `lua_sandbox.py`.

## Build / Install Commands

```bash
# Create venv and install project + pinned deps
uv venv --python 3.14
uv pip install -e .
uv pip install "https://github.com/awdemos/replicanta/releases/download/v0.1.0/scallopy-0.2.5-cp314-cp314-manylinux_2_39_x86_64.whl#sha256=ddc8d190a55681281f50dffe9f12ef1e04b90a38e1196b786ac2ddb9d7ec51be"
```

## Test Commands

```bash
# Run full test suite
pytest tests -q

# Run a single module
pytest tests/test_hooks.py -q
```

## Lint / Format

```bash
ruff check --ignore I001,UP017 .
ruff format --check .
```

## Running the App

```bash
# TUI
.venv/bin/replicanta

# Web UI
.venv/bin/replicanta --web
```

## CI / Dagger

```bash
# Full CI pipeline (lint + test)
dagger call ci --source=. --progress=plain

# Individual Dagger functions
dagger call test --source=.
dagger call lint --source=.
```

## Gotchas

- `ruff` target is `py312`; newer syntax is accepted but import style is intentionally left un-enforced (`I001` ignored).
- `requirements-ci.txt` is exported from `uv.lock` with hashes; use `uv export --frozen --no-dev -o requirements-ci.txt` to refresh.
- Voice/listen/vision extras are optional; the core install does not include them.
- The Scallopy wheel hash is hard-coded in `ci/main.go` and `readme.md`; update both if the wheel changes.
