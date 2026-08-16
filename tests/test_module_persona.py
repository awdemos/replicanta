from pathlib import Path

import pytest

from replicanta.modules import PersonaService
from replicanta.organism import BeliefStore


def test_persona_service_register_and_list():
    svc = PersonaService(BeliefStore(Path("/tmp")))
    svc.register({
        "name": "se",
        "description": "software engineer",
        "prompt": "You are an engineer.",
        "beliefs": [],
    })
    assert svc.list() == ["se"]


def test_persona_service_activate(tmp_path):
    store = BeliefStore(tmp_path)
    config = {}
    svc = PersonaService(store, config=config)
    svc.register({
        "name": "se",
        "description": "software engineer",
        "prompt": "You are an engineer.",
        "beliefs": [["self", "style", "terse"]],
    })
    svc.activate("se")
    assert config.get("persona", {}).get("active") == "se"
    assert ("self", "style", "terse") in store.beliefs()
    assert any("se" in m.get("text", "") for m in store.memory)


def test_persona_prompt_fragment(tmp_path):
    store = BeliefStore(tmp_path)
    svc = PersonaService(store)
    svc.register({
        "name": "se",
        "description": "software engineer",
        "prompt": "You are an engineer.",
        "beliefs": [],
    })
    assert svc.prompt_fragment() == ""
    svc.activate("se")
    assert svc.prompt_fragment() == "You are an engineer."
