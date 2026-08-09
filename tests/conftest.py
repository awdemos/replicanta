"""Shared test fixtures: the cached ollama voice state and the extension
registry are module-global, so reset them around every test to keep
reachability and registry contents deterministic."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import extensions
import narration
import pytest


@pytest.fixture(autouse=True)
def _reset_voice_state():
    narration.reset_voice()
    extensions.reset()
    yield
    narration.reset_voice()
    extensions.reset()
