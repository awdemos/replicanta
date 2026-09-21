"""Service- and module-level tests for the fly-brain capability bridge."""

import threading
import time
from pathlib import Path

import pytest

from replicanta.fly_brain import FlyBrainService
from replicanta.modules import ModuleLoader

REPO_MODULES = Path(__file__).parent.parent / "modules"

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
    print("bank: /tmp/fake-bank.jsonl (2 entries)")
    print("  #1   digits     l5  val=0.9500 test=0.9400 policy_v=1")
    print("  #2   digits     l5  val=0.9400 test=0.9300 policy_v=2")
    print("policy v2 (from 2 top entries): center sr=0.900 leak=0.400 ridge=0.0100")
elif has("optimize"):
    print(json.dumps({
        "task": "digits", "level": "l5", "trials": 16, "best_config": "sr=0.9",
        "median_val": 0.95, "seed_spread": 0.01, "default_median_val": 0.92,
        "test": 0.975, "default_test": 0.9278, "improvement": 0.0472,
        "curriculum_gain": 0.01, "bank": "/tmp/fake-bank.jsonl",
        "bank_entries": 2, "events": 100,
    }))
elif has("adapt"):
    print(json.dumps({
        "task": "digits", "mode": "l4_deployment_rehearsal",
        "l4_naive_val": 0.81, "l4_gated_val": 0.85, "gate_advantage": 0.04,
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


@pytest.fixture
def fake(tmp_path):
    return FlyBrainService(binary=_write_bin(tmp_path, "wetware", FAKE_BIN))


@pytest.fixture
def failing(tmp_path):
    return FlyBrainService(binary=_write_bin(tmp_path, "wetware-fail", FAIL_BIN))


# -- binary resolution ---------------------------------------------------------


def test_available_reports_resolved_path(fake, tmp_path):
    assert fake.available() is True
    assert fake.path() == str(tmp_path / "wetware")


def test_missing_binary_is_unavailable(tmp_path):
    svc = FlyBrainService(binary=str(tmp_path / "nope"))
    assert svc.available() is False
    assert svc.path() is None
    with pytest.raises(RuntimeError, match="wetware binary not found"):
        svc.info()
    assert "no binary" in svc.status()


# -- sync API -------------------------------------------------------------------


def test_info_returns_dict_proxy(fake):
    info = fake.info()
    assert info.neurons == 600
    assert info.source == "synthetic sample"


def test_optimize_sync_parses_report_and_summarizes(fake):
    report = fake.optimize(wait=True)
    assert report.task == "digits"
    assert report.improvement == pytest.approx(0.0472)
    last = fake.last()
    assert last.ok is True
    assert "digits l5 done" in last.text
    assert "improvement +0.0472" in last.text
    assert "bank 2 entries" in last.text


def test_optimize_validates_task_and_level(fake):
    with pytest.raises(ValueError, match="unknown task"):
        fake.optimize("chess", wait=True)
    with pytest.raises(ValueError, match="unknown level"):
        fake.optimize("digits", level="l9", wait=True)


def test_adapt_sync_summarizes_gate(fake):
    report = fake.adapt(wait=True)
    assert report.gate_advantage == pytest.approx(0.04)
    assert "gated 0.8500 vs naive 0.8100" in fake.last().text
    assert "3 adaptations" in fake.last().text
    assert "1 rollbacks" in fake.last().text


def test_bank_returns_text(fake):
    assert "2 entries" in fake.bank()


def test_failure_raises_with_stderr_tail(failing):
    with pytest.raises(RuntimeError, match="connectome cache corrupted"):
        failing.optimize(wait=True)


def test_failure_summary_delivered_async(failing):
    delivered = []
    done = threading.Event()
    failing.optimize(on_done=lambda text: (delivered.append(text), done.set()))
    assert done.wait(5.0)
    assert delivered[0].startswith("fly brain: optimize failed:")
    assert "connectome cache corrupted" in delivered[0]
    assert failing.last().ok is False


# -- async API -------------------------------------------------------------------


def test_optimize_async_delivers_summary(fake):
    delivered = []
    done = threading.Event()
    assert fake.optimize(on_done=lambda text: (delivered.append(text), done.set())) is True
    assert done.wait(5.0)
    assert "digits l5 done" in delivered[0]
    assert fake.running() is False


def test_second_async_run_rejected_while_running(fake):
    fake._running = True  # simulate an in-flight run
    with pytest.raises(RuntimeError, match="already running"):
        fake.optimize(on_done=lambda _text: None)


def test_async_callback_runs_under_lua_lock(fake):
    delivered = []
    done = threading.Event()
    lock = threading.Lock()
    svc = FlyBrainService(binary=fake._binary, lua_lock=lock)
    svc.optimize(on_done=lambda text: (delivered.append(lock.locked()), done.set()))
    assert done.wait(5.0)
    assert delivered == [True]  # callback held the lock while running


# -- module integration -----------------------------------------------------------


def _load_fly_brain(tmp_path, logs):
    import shutil

    src = REPO_MODULES
    if src.is_dir():
        shutil.copytree(src, tmp_path / "modules", dirs_exist_ok=True)
    loader = ModuleLoader(
        tmp_path / "modules",
        organism=None,
        modules_config={"enabled": ["base", "fly-brain"]},
        emit=logs.append,
    )
    loader.load_all()
    return loader


def test_fly_brain_module_loads_and_registers_brain(tmp_path):
    loader = _load_fly_brain(tmp_path, [])
    assert "fly-brain" in loader.modules, loader.warnings
    brain = loader.registry.get("brain")
    assert brain is not None
    status = brain.status()
    assert "fly brain bridge:" in status


def test_fly_brain_utterance_dispatch_logs(tmp_path):
    logs = []
    loader = _load_fly_brain(tmp_path, logs)
    hooks = loader.registry.get("hooks")
    hooks.emit("utterance", 'brain.optimize("chess")')  # unknown task: graceful failure log
    assert any("fly-brain: dispatched" in m for m in logs)
    assert any("could not start" in m for m in logs)


def test_fly_brain_slash_command_status(tmp_path):
    loader = _load_fly_brain(tmp_path, [])
    commands = loader.registry.get("commands")
    out = commands.dispatch("/brain", ["status"])
    assert "fly brain bridge:" in out


def test_fly_brain_events_declared(tmp_path):
    loader = _load_fly_brain(tmp_path, [])
    events = loader.registry.get("hooks")
    assert "flybrain_done" in events.known()
    assert "flybrain_error" in events.known()


def test_completion_event_reaches_bus(tmp_path):
    logs = []
    loader = _load_fly_brain(tmp_path, logs)
    events = loader.registry.get("hooks")
    got = []
    events.on("flybrain_done", lambda text: got.append(text))
    svc = loader.registry.get("flybrain")
    svc._binary = _write_bin(tmp_path, "wetware", FAKE_BIN)
    brain = loader.registry.get("brain")
    assert brain.optimize("digits", 8, "l3") is True
    deadline = time.time() + 10.0
    while time.time() < deadline and not got:
        time.sleep(0.05)
    assert got, f"no flybrain_done event; logs={logs}"
    assert "digits" in got[0]
    assert "done" in got[0]
