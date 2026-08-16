from pathlib import Path

import pytest

from replicanta.modules import PersonaService


def test_persona_service_activate_and_list():
    from replicanta.organism import BeliefStore

    store = BeliefStore(Path("/tmp"))
    svc = PersonaService(store)
    svc.register({
        "name": "se",
        "description": "engineer",
        "prompt": "You are an engineer.",
        "beliefs": [],
    })
    assert svc.list() == ["se"]
    svc.activate("se")
    assert svc.active()["name"] == "se"
