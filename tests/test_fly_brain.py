"""Module-level tests for the pure-Lua fly-brain capability.

The module spawns the (fake) wetware CLI through ctx.process and parses
reports with ctx.json — no Python service involved. The fake binaries are
committed fixtures written into tmp dirs, selected via WETWARE_BIN.
"""

import shutil
import time
from pathlib import Path

import pytest

from replicanta.modules import ModuleLoader

MODULES_SRC = Path(__file__).parent.parent / "modules"

FAKE_BIN = """#!/usr/bin/env python3
import json, sys

args = sys.argv[1:]

def has(*xs):
    return any(x in args for x in xs)

if has("info"):
    print(json.dumps({
        "source": "synthetic sample", "neurons": 600, "connections": 4000,
        "density": 0.0111, "mean_synapses_per_connection": 2.5,
    }))
elif has("bank"):
    print(json.dumps({
        "policy": "greedy", "entries": 0,
    }))
elif has("run"):
    print(json.dumps({
        "task": "digits", "trials": 16, "best_config": "sr=0.9",
        "median_val": 0.95, "seed_spread": 0.01, "default_median_val": 0.92,
        "test": 0.975, "default_test": 0.9278, "improvement": 0.0472,
        "bank": "/tmp/fake-bank.jsonl",
        "bank_entries": 2, "events": 100,
    }))
elif has("adapt"):
    print(json.dumps({
        "l4_gated_val": 0.88, "l4_naive_val": 0.82, "gate_advantage": 0.06,
        "adaptations": 3, "rollbacks": 1,
    }))
else:
    sys.stderr.write("unknown invocation")
    sys.exit(1)
"""

FAIL_BIN = """#!/usr/bin/env python3
import sys
sys.stderr.write("connectome cache corrupted")
sys.exit(1)
"""


def _write_bin(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def _wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _load(tmp_path, monkeypatch, enabled=None, binary=None, organism=None):
    monkeypatch.setenv("WETWARE_BIN", binary)
    target = tmp_path / "modules"
    shutil.copytree(MODULES_SRC, target)
    loader = ModuleLoader(target, organism=organism, modules_config={"enabled": enabled or ["base", "fly-brain"]})
    loader.load_all()
    return loader


def _organism(tmp_path):
    from replicanta.organism import Organism

    org_dir = tmp_path / "org"
    org_dir.mkdir(parents=True)
    (org_dir / "organism.scl").write_text("type bel(x: String, a: String, v: String)\n")
    org = Organism(org_dir)
    org.load()
    return org


# -- availability ----------------------------------------------------------------


def test_status_reports_ready_with_binary(tmp_path, monkeypatch):
    bin_path = _write_bin(tmp_path, "wetware", FAKE_BIN)
    loader = _load(tmp_path, monkeypatch, binary=bin_path)
    brain = loader.registry.get("brain")
    assert brain.available() is True
    status = brain.status()
    assert "ready" in status
    assert bin_path in status


def test_status_reports_missing_binary(tmp_path, monkeypatch):
    monkeypatch.delenv("WETWARE_BIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # keep the ~/code search empty
    target = tmp_path / "modules"
    shutil.copytree(MODULES_SRC, target)
    loader = ModuleLoader(target, organism=None, modules_config={"enabled": ["base", "fly-brain"]})
    loader.load_all()
    brain = loader.registry.get("brain")
    assert brain.available() is False
    assert "no binary" in brain.status()


# -- runs -------------------------------------------------------------------------


def test_sync_run_returns_report_and_sets_last(tmp_path, monkeypatch):
    bin_path = _write_bin(tmp_path, "wetware", FAKE_BIN)
    loader = _load(tmp_path, monkeypatch, binary=bin_path)
    brain = loader.registry.get("brain")
    report = brain.run("digits", True)
    assert report.task == "digits"
    last = brain.last()
    assert last.ok is True
    assert "97.50" in last.text or "0.9750" in last.text
    assert "bank 2 entries" in last.text


def test_sync_adapt_summary(tmp_path, monkeypatch):
    bin_path = _write_bin(tmp_path, "wetware", FAKE_BIN)
    loader = _load(tmp_path, monkeypatch, binary=bin_path)
    brain = loader.registry.get("brain")
    report = brain.adapt(True)
    assert report.l4_gated_val == 0.88
    assert "gated 0.8800 vs naive 0.8200" in brain.last().text


def test_unknown_task_raises_lua_catchable(tmp_path, monkeypatch):
    bin_path = _write_bin(tmp_path, "wetware", FAKE_BIN)
    loader = _load(tmp_path, monkeypatch, binary=bin_path)
    brain = loader.registry.get("brain")
    with pytest.raises(Exception, match="unknown task"):
        brain.run("chess", True)


def test_async_run_delivers_summary_and_events(tmp_path, monkeypatch):
    bin_path = _write_bin(tmp_path, "wetware", FAKE_BIN)
    loader = _load(tmp_path, monkeypatch, binary=bin_path)
    brain = loader.registry.get("brain")
    seen = []
    hooks = loader.registry.get("hooks")
    hooks.on("flybrain_done", seen.append)
    assert brain.run("digits") is True
    assert brain.running() is True
    assert _wait_for(lambda: len(seen) == 1)
    assert "digits done" in seen[0]
    assert brain.running() is False


def test_failure_delivery_marks_error(tmp_path, monkeypatch):
    bin_path = _write_bin(tmp_path, "wetware-fail", FAIL_BIN)
    loader = _load(tmp_path, monkeypatch, binary=bin_path)
    brain = loader.registry.get("brain")
    seen = []
    loader.registry.get("hooks").on("flybrain_error", seen.append)
    assert brain.run("digits") is True
    assert _wait_for(lambda: len(seen) == 1)
    assert "failed" in seen[0]
    assert "connectome cache corrupted" in seen[0]
    assert brain.last().ok is False


# -- persistence into the organism --------------------------------------------------


def test_run_result_is_remembered(tmp_path, monkeypatch):
    """A finished run must land in the organism's memory, not just the log."""
    bin_path = _write_bin(tmp_path, "wetware", FAKE_BIN)
    org = _organism(tmp_path / "o")
    loader = _load(tmp_path, monkeypatch, binary=bin_path, organism=org)
    brain = loader.registry.get("brain")
    brain.run("digits", True)
    assert any(m["kind"] == "flybrain" for m in org.store.memory)
    assert any("0.9750" in m["text"] for m in org.store.memory)


def test_utterance_dispatches_brain_run(tmp_path, monkeypatch):
    bin_path = _write_bin(tmp_path, "wetware", FAKE_BIN)
    org = _organism(tmp_path / "o")
    loader = _load(tmp_path, monkeypatch, binary=bin_path, organism=org)
    hooks = loader.registry.get("hooks")
    hooks.emit("utterance", 'brain.optimize("digits")')
    assert _wait_for(lambda: any(m["kind"] == "flybrain" for m in org.store.memory))


# -- commands and snapshot -----------------------------------------------------------


def test_brain_bank_command(tmp_path, monkeypatch):
    bin_path = _write_bin(tmp_path, "wetware", FAKE_BIN)
    loader = _load(tmp_path, monkeypatch, binary=bin_path)
    commands = loader.registry.get("commands")
    assert "policy: greedy" in commands.dispatch("/brain", ["bank"])
    assert "no finished runs" in commands.dispatch("/brain", ["last"])
    loader.registry.get("brain").run("digits", True)
    assert "0.9750" in commands.dispatch("/brain", ["last"])


def test_snapshot_includes_last_run_summary(tmp_path, monkeypatch):
    from replicanta import narration

    bin_path = _write_bin(tmp_path, "wetware", FAKE_BIN)
    org = _organism(tmp_path / "o")
    loader = _load(tmp_path, monkeypatch, binary=bin_path, organism=org)
    org.module_loader = loader
    brain = loader.registry.get("brain")
    brain.run("digits", True)

    snap = narration.state_snapshot(org)
    assert snap["flybrain"] is True
    assert "0.9750" in snap["brain_last"]
    assert any("Last run: fly brain: digits done" in line for line in narration._brain_lines(snap))
