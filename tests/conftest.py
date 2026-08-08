"""Shared test fixtures: the cached ollama voice state is module-global,
so reset it around every test to keep reachability deterministic."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import narration
import pytest


@pytest.fixture(autouse=True)
def _reset_voice_state():
    narration.reset_voice()
    yield
    narration.reset_voice()
