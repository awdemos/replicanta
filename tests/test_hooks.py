"""Lua-hooks feature: scripts/*.lua define on_birth/on_cycle/on_learned/
on_utterance/on_fade; the engine fires them with a ctx table (state +
activity counters + safe actuators), sandboxed and exception-proof."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from hooks import HookEngine, scripts_dir_for
from organism import Organism
from probe import SystemProbe


@pytest.fixture
def scripts(tmp_path):
    d = tmp_path / "scripts"
    d.mkdir()
    return d


def _org(tmp_path):
    org = Organism(tmp_path, probe=SystemProbe(proc="/nonexistent/proc",
                                               sys="/nonexistent/sys"))
    org.load()
    return org


def test_no_scripts_is_a_quiet_noop(scripts):
    engine = HookEngine(scripts)
    assert engine.scripts == []
    engine.fire("cycle", None)          # nothing happens, nothing breaks


def test_hook_receives_ctx_and_logs(scripts, tmp_path):
    (scripts / "hello.lua").write_text(
        "function on_learned(ctx)\n"
        "  ctx.log('learned from: ' .. ctx.text .. ' at cycle '.. ctx.cycle)\n"
        "end\n")
    emitted = []
    engine = HookEngine(scripts, emit=emitted.append)
    org = _org(tmp_path)
    engine.fire("learned", org, text="my name is sam")
    assert emitted == ["learned from: my name is sam at cycle 0"]


def test_ctx_reports_state_and_activity(scripts, tmp_path):
    (scripts / "probe.lua").write_text(
        "function on_cycle(ctx)\n"
        "  ctx.log(ctx.state .. ' ' .. ctx.mood .. ' '\n"
        "          .. ctx.belief_count .. ' beliefs, learned='\n"
        "          .. tostring(ctx.activity.facts_learned))\n"
        "end\n")
    emitted = []
    engine = HookEngine(scripts, emit=emitted.append)
    org = _org(tmp_path)
    org.hear("my name is sam")
    engine.fire("cycle", org)
    assert emitted[0].startswith("wake ")
    assert "learned=1" in emitted[0]


def test_set_chaos_actuator_clamps(scripts, tmp_path):
    (scripts / "chaos.lua").write_text(
        "function on_cycle(ctx) ctx.set_chaos(9) end\n")
    engine = HookEngine(scripts)
    org = _org(tmp_path)
    engine.fire("cycle", org)
    assert org.store.chaos == 1.0


def test_focus_actuator(scripts, tmp_path):
    (scripts / "focus.lua").write_text(
        "function on_cycle(ctx) ctx.focus('mood') end\n")
    engine = HookEngine(scripts)
    org = _org(tmp_path)
    engine.fire("cycle", org)
    assert org.window.focus_attr == "mood"
    assert org.store.attention == org.window.pairs


def test_broken_script_emits_error_never_raises(scripts, tmp_path):
    (scripts / "bad.lua").write_text(
        "function on_cycle(ctx) error('boom') end\n")
    emitted = []
    engine = HookEngine(scripts, emit=emitted.append)
    engine.fire("cycle", _org(tmp_path))
    assert len(emitted) == 1 and "bad.lua" in emitted[0]


def test_sandbox_blocks_os(scripts, tmp_path):
    (scripts / "evil.lua").write_text(
        "function on_cycle(ctx)\n"
        "  if os == nil then ctx.log('no os') else ctx.log('PWNED') end\n"
        "end\n")
    emitted = []
    engine = HookEngine(scripts, emit=emitted.append)
    engine.fire("cycle", _org(tmp_path))
    assert emitted == ["no os"]


def test_reload_picks_up_new_scripts(scripts, tmp_path):
    engine = HookEngine(scripts)
    assert engine.scripts == []
    (scripts / "late.lua").write_text(
        "function on_birth(ctx) ctx.log('hi') end\n")
    engine.reload()
    assert [s.name for s in engine.scripts] == ["late.lua"]
    emitted = []
    engine.emit = emitted.append
    engine.fire("birth", _org(tmp_path))
    assert emitted == ["hi"]


def test_scripts_dir_resolution(tmp_path):
    nested = tmp_path / "organisms" / "fern"
    nested.mkdir(parents=True)
    assert scripts_dir_for(nested) == tmp_path / "scripts"
    assert scripts_dir_for(tmp_path) == tmp_path / "scripts"


# -- organism wiring ------------------------------------------------------------

def test_organism_fires_birth_learned_and_utterance(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "all.lua").write_text(
        "function on_birth(ctx) ctx.log('born as ' .. ctx.organism) end\n"
        "function on_learned(ctx) ctx.log('learned') end\n"
        "function on_utterance(ctx) ctx.log('said: ' .. ctx.text) end\n")
    emitted = []
    org_dir = tmp_path / "organisms" / "default"
    org_dir.mkdir(parents=True)
    org = Organism(org_dir, probe=SystemProbe(proc="/nonexistent/proc",
                                              sys="/nonexistent/sys"))
    org.hooks.emit = emitted.append
    org.load()
    assert emitted == ["born as default"]
    org.hear("my name is sam")
    assert "learned" in emitted
    org.store.record_chat("org", "hello there")
    assert "said: hello there" in emitted
    org.store.record_chat("user", "not an utterance")
    assert emitted.count("said: not an utterance") == 0


def test_cycle_hook_fires_on_transition(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "c.lua").write_text(
        "function on_cycle(ctx) ctx.log('-> ' .. ctx.text) end\n")
    emitted = []
    org = Organism(tmp_path, probe=SystemProbe(proc="/nonexistent/proc",
                                               sys="/nonexistent/sys"),
                   wake_seconds=0)      # transition on the first tick
    org.load()
    org.hooks.emit = emitted.append
    org.tick(1.0)
    assert "-> sleep" in emitted


# -- /lua on-demand runs ------------------------------------------------------

def test_run_executes_main_with_ctx(scripts, tmp_path):
    (scripts / "once.lua").write_text(
        "function main(ctx)\n"
        "  ctx.log('ran ' .. ctx.event .. ' as ' .. ctx.organism)\n"
        "end\n")
    emitted = []
    engine = HookEngine(scripts, emit=emitted.append)
    status = engine.run("once.lua", _org(tmp_path))
    assert status == "lua: ran once.lua"
    assert emitted and emitted[0].startswith("ran lua as ")


def test_run_without_main_still_executes(scripts, tmp_path):
    (scripts / "bare.lua").write_text("x = 1 + 1\n")
    engine = HookEngine(scripts)
    assert engine.run("bare.lua", _org(tmp_path)) == "lua: ran bare.lua"


def test_run_rejects_traversal_and_non_lua(scripts, tmp_path):
    engine = HookEngine(scripts)
    org = _org(tmp_path)
    assert "bad script name" in engine.run("../secrets.lua", org)
    assert "bad script name" in engine.run("notes.txt", org)


def test_run_missing_script(scripts, tmp_path):
    engine = HookEngine(scripts)
    status = engine.run("ghost.lua", _org(tmp_path))
    assert status.startswith("/lua: no ghost.lua")


def test_run_broken_script_returns_error_never_raises(scripts, tmp_path):
    (scripts / "bad.lua").write_text("error('boom')\n")
    engine = HookEngine(scripts)
    status = engine.run("bad.lua", _org(tmp_path))
    assert status.startswith("bad.lua:") and "boom" in status


def test_run_shares_the_sandbox(scripts, tmp_path):
    """/lua scripts are sandboxed exactly like event hooks."""
    (scripts / "evil.lua").write_text(
        "function main(ctx)\n"
        "  if os == nil then ctx.log('no os') else ctx.log('PWNED') end\n"
        "end\n")
    emitted = []
    engine = HookEngine(scripts, emit=emitted.append)
    engine.run("evil.lua", _org(tmp_path))
    assert emitted == ["no os"]
