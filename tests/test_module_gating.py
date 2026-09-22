"""Capability gating: disabled modules must not reach the entity.

The Python capability services (arm/flybrain/doom) are registered in the
module loader's registry unconditionally, so service presence alone cannot
advertise a capability — with tendon-hand disabled the entity would still
read the ROBOT HAND prompt section and keep emitting hand.move calls.
Only modules that actually loaded may surface in the snapshot/prompts.
"""

import shutil
from pathlib import Path

from replicanta import narration
from replicanta.modules import ModuleLoader
from replicanta.organism import Organism

MODULES_SRC = Path(__file__).parent.parent / "modules"


def _loader(tmp_path, enabled):
    target = tmp_path / f"modules-{len(list(tmp_path.iterdir()))}"
    shutil.copytree(MODULES_SRC, target)
    logs = []
    loader = ModuleLoader(target, organism=None, modules_config={"enabled": enabled}, emit=logs.append)
    loader.load_all()
    return loader


def _organism(tmp_path):
    org_dir = tmp_path / "org"
    org_dir.mkdir()
    (org_dir / "organism.scl").write_text("type bel(x: String, a: String, v: String)\n")
    org = Organism(org_dir)
    org.load()
    return org


def test_disabled_hand_module_is_not_advertised(tmp_path):
    org = _organism(tmp_path)
    loader = _loader(tmp_path, ["base"])
    assert loader.registry.get("arm") is not None  # the Python service is always there
    org.module_loader = loader

    snap = narration.state_snapshot(org)
    assert snap["arm"] is False
    assert narration._hand_lines(snap) == []


def test_enabled_hand_module_is_advertised(tmp_path):
    org = _organism(tmp_path)
    org.module_loader = _loader(tmp_path, ["base", "tendon-hand"])

    snap = narration.state_snapshot(org)
    assert snap["arm"] is True
    assert len(narration._hand_lines(snap)) > 0


def test_disabled_fly_brain_and_doom_are_not_advertised(tmp_path):
    org = _organism(tmp_path)
    org.module_loader = _loader(tmp_path, ["base"])

    snap = narration.state_snapshot(org)
    assert snap["flybrain"] is False
    assert narration._brain_lines(snap) == []
    assert snap["doom"] is False
    assert narration._doom_lines(snap) == []


def test_enabled_doom_module_reports_game_state(tmp_path, monkeypatch):
    # The service runs the stub binary double: no C toolchain or WAD needed.
    wad = tmp_path / "doom1.wad"
    wad.write_bytes(b"PWAD fake")
    monkeypatch.setenv("DOOM_ASCII_BIN", str(Path(__file__).parent / "fixtures" / "doom_ascii_stub.py"))
    monkeypatch.setenv("DOOM_WAD", str(wad))
    monkeypatch.setenv("DOOM_ASCII_ARGS", "--interval 0.02")
    org = _organism(tmp_path)
    loader = _loader(tmp_path, ["base", "doom-ascii"])
    org.module_loader = loader
    loader.registry.get("doom").start("box")

    snap = narration.state_snapshot(org)
    assert snap["doom"] is True
    assert "doom-ascii" in snap["doom_status"]
