"""System-probe feature: the organism perceives the host machine by
sampling /proc and /sys (the same sources btm reads) instead of a toy
object/color world. Covers SystemProbe parsing + quantization, the
distress() stress coupling, BeliefStore.observe(), Organism.sense(),
and migration away from legacy object beliefs."""

import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from organism import BeliefStore, Organism
from probe import SystemProbe

STAT_SAMPLE = (
    "cpu  22477106 4036 7338939 541387304 409658 964615 308654 0 0 0\n"
    "cpu0 2042889 1755 503532 33117919 30077 54521 32055 0 0 0\n"
)
MEMINFO = (
    "MemTotal:       200000 kB\n"
    "MemAvailable:   100000 kB\n"
)
LOADAVG = "0.50 0.55 0.60 2/100 1000\n"
UPTIME = "3600.00 5000.00\n"


# -- fake /proc + /sys trees ---------------------------------------------

def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def proc(tmp_path):
    p = tmp_path / "proc"
    _write(p / "stat", STAT_SAMPLE)
    _write(p / "meminfo", MEMINFO)
    _write(p / "loadavg", LOADAVG)
    _write(p / "uptime", UPTIME)
    return p


@pytest.fixture
def sys_tree(tmp_path):
    s = tmp_path / "sys"
    _write(s / "class/thermal/thermal_zone0/temp", "50000\n")
    _write(s / "class/power_supply/BAT1/capacity", "80\n")
    _write(s / "class/power_supply/BAT1/status", "Charging\n")
    return s


def _probe(proc, sys_tree, ncpu=4, statvfs=None, clock=None):
    return SystemProbe(proc=proc, sys=sys_tree, ncpu=ncpu, statvfs=statvfs,
                       clock=clock)


def _utc_midnight():
    return datetime(2026, 8, 7, 0, 5, tzinfo=timezone.utc)


def _disk(blocks, bavail, frsize=1024):
    return lambda _p: SimpleNamespace(
        f_blocks=blocks, f_bavail=bavail, f_frsize=frsize)


# -- raw metric parsing ---------------------------------------------------

def test_cpu_percent_needs_two_samples(proc, sys_tree):
    probe = _probe(proc, sys_tree)
    assert probe.snapshot()["cpu_percent"] is None  # first read warms up


def test_cpu_percent_delta(proc, sys_tree):
    probe = _probe(proc, sys_tree)
    probe.snapshot()  # warm
    # Second read: idle delta 0, total delta 100 -> 100% busy
    _write(proc / "stat", "cpu  22478106 4036 7338939 541387304 409658 964615 308654 0 0 0\n")
    snap = probe.snapshot()
    assert snap["cpu_percent"] == pytest.approx(100.0)


def test_mem_percent_from_meminfo(proc, sys_tree):
    snap = _probe(proc, sys_tree).snapshot()
    assert snap["mem_percent"] == pytest.approx(50.0)  # 100k used / 200k


def test_load_ratio_from_loadavg_ncpu(proc, sys_tree):
    snap = _probe(proc, sys_tree, ncpu=4).snapshot()
    assert snap["load_ratio"] == pytest.approx(0.125)  # 0.50 / 4


def test_uptime_seconds(proc, sys_tree):
    snap = _probe(proc, sys_tree).snapshot()
    assert snap["uptime_s"] == pytest.approx(3600.0)


def test_max_temp_across_zones(proc, sys_tree):
    _write(proc.parent / "sys/class/thermal/thermal_zone1/temp", "82000\n")
    snap = _probe(proc, sys_tree).snapshot()
    assert snap["temp_c"] == pytest.approx(82.0)


def test_battery_capacity_and_status(proc, sys_tree):
    snap = _probe(proc, sys_tree).snapshot()
    assert snap["battery_percent"] == 80
    assert snap["battery_charging"] is True


def test_missing_files_yield_none(tmp_path):
    probe = SystemProbe(proc=tmp_path / "noproc", sys=tmp_path / "nosys")
    snap = probe.snapshot()
    assert snap["cpu_percent"] is None
    assert snap["mem_percent"] is None
    assert snap["load_ratio"] is None
    assert snap["uptime_s"] is None
    assert snap["temp_c"] is None
    assert snap["battery_percent"] is None
    assert snap["battery_charging"] is None


# -- quantization to beliefs ----------------------------------------------

def test_load_level_thresholds(proc, sys_tree):
    probe = _probe(proc, sys_tree, ncpu=4)
    _write(proc / "loadavg", "0.50 0.55 0.60 2/100 1000\n")
    assert probe.beliefs(probe.snapshot())[("cpu", "load", "low")]  # 0.125
    _write(proc / "loadavg", "2.00 2.00 2.00 2/100 1000\n")
    assert probe.beliefs(probe.snapshot())[("cpu", "load", "mid")]  # 0.5
    _write(proc / "loadavg", "5.00 5.00 5.00 2/100 1000\n")
    assert probe.beliefs(probe.snapshot())[("cpu", "load", "high")]  # 1.25


def test_mem_usage_level_thresholds(proc, sys_tree):
    probe = _probe(proc, sys_tree)
    _write(proc / "meminfo", "MemTotal:       100000 kB\nMemAvailable:   90000 kB\n")
    assert probe.beliefs(probe.snapshot())[("mem", "usage", "low")]  # 10%
    _write(proc / "meminfo", "MemTotal:       100000 kB\nMemAvailable:   50000 kB\n")
    assert probe.beliefs(probe.snapshot())[("mem", "usage", "mid")]  # 50%
    _write(proc / "meminfo", "MemTotal:       100000 kB\nMemAvailable:   10000 kB\n")
    assert probe.beliefs(probe.snapshot())[("mem", "usage", "high")]  # 90%


def test_disk_space_level_thresholds(proc, sys_tree):
    probe = _probe(proc, sys_tree, statvfs=_disk(1000, 100))  # 10% free -> mid
    assert probe.beliefs(probe.snapshot())[("disk", "space", "mid")]
    probe = _probe(proc, sys_tree, statvfs=_disk(1000, 50))  # 5% free -> low
    assert probe.beliefs(probe.snapshot())[("disk", "space", "low")]
    probe = _probe(proc, sys_tree, statvfs=_disk(1000, 500))  # 50% free -> ok
    assert probe.beliefs(probe.snapshot())[("disk", "space", "ok")]


def test_temp_level_thresholds(proc, sys_tree):
    probe = _probe(proc, sys_tree)
    for raw, level in [("30000", "cool"), ("60000", "warm"), ("82000", "hot")]:
        _write(proc.parent / "sys/class/thermal/thermal_zone0/temp", raw + "\n")
        assert probe.beliefs(probe.snapshot())[("temp", "cpu", level)]


def test_battery_level_and_charging(proc, sys_tree):
    probe = _probe(proc, sys_tree)
    _write(proc.parent / "sys/class/power_supply/BAT1/capacity", "15\n")
    b = probe.beliefs(probe.snapshot())
    assert b[("battery", "level", "low")]
    assert b[("battery", "charging", "true")]


def test_no_battery_no_battery_beliefs(proc, sys_tree):
    for f in list((proc.parent / "sys/class/power_supply/BAT1").glob("*")):
        f.unlink()
    b = probe_beliefs(proc, sys_tree)
    assert not any(obj == "battery" for (obj, _a, _v) in b)


def test_uptime_level_thresholds(proc, sys_tree):
    probe = _probe(proc, sys_tree)
    for raw, level in [("100.00", "brief"), ("7200.00", "day"), ("200000.00", "long")]:
        _write(proc / "uptime", raw + "\n")
        assert probe.beliefs(probe.snapshot())[("system", "uptime", level)]


def test_all_belief_values_are_valid_symbols(proc, sys_tree):
    """Every quantized value must pass the store's VALID_VALUE_RE so beliefs
    can be added without raising ValueError."""
    from organism import VALID_VALUE_RE
    b = probe_beliefs(proc, sys_tree)
    for (_o, _a, v) in b:
        assert VALID_VALUE_RE.match(v), f"invalid belief value {v!r}"


# -- UTC clock ---------------------------------------------------------------

def test_clock_utc_formats_hhmm(proc, sys_tree):
    probe = _probe(proc, sys_tree,
                   clock=lambda: datetime(2026, 8, 7, 14, 30,
                                          tzinfo=timezone.utc))
    assert probe.clock_utc() == "14:30 UTC"


def test_clock_naive_is_treated_as_utc(proc, sys_tree):
    """A naive clock (no tzinfo) is assumed to already be UTC."""
    snap = _probe(proc, sys_tree,
                  clock=lambda: datetime(2026, 8, 7, 14, 30)  # noqa: DTZ001
                  ).snapshot()
    assert snap["clock_hour"] == 14
    assert snap["clock_minute"] == 30


def test_clock_converts_local_to_utc(proc, sys_tree):
    """A clock in another timezone is normalized to UTC: the organism
    perceives universal time, not the host's local wall clock."""
    local = datetime(2026, 8, 7, 14, 30, tzinfo=timezone(timedelta(hours=5)))
    probe = _probe(proc, sys_tree, clock=lambda: local)
    assert probe.clock_utc() == "09:30 UTC"


def test_clock_midnight_belief(proc, sys_tree):
    probe = _probe(proc, sys_tree, clock=_utc_midnight)
    assert probe.beliefs(probe.snapshot())[("time", "hour", "midnight")] == 0.9


def test_clock_hour_belief_is_a_word_not_digits(proc, sys_tree):
    probe = _probe(proc, sys_tree,
                   clock=lambda: datetime(2026, 8, 7, 17, 42,
                                          tzinfo=timezone.utc))
    b = probe.beliefs(probe.snapshot())
    assert b[("time", "hour", "seventeen")] == 0.9
    assert ("time", "hour", "17") not in b


# -- distress() stress coupling -------------------------------------------

def test_distress_zero_on_calm_system(proc, sys_tree):
    probe = _probe(proc, sys_tree)
    assert probe.distress(probe.snapshot()) == 0.0


def test_distress_from_adverse_metrics(proc, sys_tree):
    _write(proc / "loadavg", "10.00 10.00 10.00 2/100 1000\n")      # high load
    _write(proc / "meminfo", "MemTotal: 100000 kB\nMemAvailable: 5000 kB\n")  # high mem
    _write(proc.parent / "sys/class/thermal/thermal_zone0/temp", "95000\n")   # hot
    probe2 = _probe(proc, sys_tree, ncpu=4, statvfs=_disk(1000, 10))          # low disk
    _write(proc.parent / "sys/class/power_supply/BAT1/capacity", "5\n")       # low battery
    amount = probe2.distress(probe2.snapshot())
    assert amount > 0.0
    assert amount <= 0.15  # capped


def test_distress_is_edge_triggered_not_stacked(proc, sys_tree):
    """Persistently adverse conditions bump stress once, then stay quiet:
    a busy host alone must never be able to pin stress in the fade zone
    (regression: per-tick distress killed the organism on load spikes)."""
    _write(proc / "loadavg", "10.00 10.00 10.00 2/100 1000\n")      # high load
    _write(proc.parent / "sys/class/thermal/thermal_zone0/temp", "95000\n")   # hot
    _write(proc.parent / "sys/class/power_supply/BAT1/capacity", "5\n")       # low battery
    probe2 = _probe(proc, sys_tree, ncpu=4, statvfs=_disk(1000, 10))
    first = probe2.distress(probe2.snapshot())
    assert first > 0.0
    for _ in range(10):
        assert probe2.distress(probe2.snapshot()) == 0.0  # no re-stacking
    # once the condition recovers and returns, it counts again
    _write(proc / "loadavg", "0.10 0.10 0.10 2/100 1000\n")
    assert probe2.distress(probe2.snapshot()) == 0.0
    _write(proc / "loadavg", "10.00 10.00 10.00 2/100 1000\n")
    assert probe2.distress(probe2.snapshot()) > 0.0


# -- BeliefStore.observe() -------------------------------------------------

def test_observe_replaces_prior_reading(store):
    store.add(("mem", "usage", "low"), 0.9)
    store.observe(("mem", "usage", "high"), 0.9)
    assert store.conf(("mem", "usage", "high")) == pytest.approx(0.9)
    assert store.conf(("mem", "usage", "low")) is None
    assert ("mem", "usage", "low") not in store.archived()  # perception, not contradiction


def test_observe_persists_across_save_load(store):
    store.observe(("cpu", "load", "high"), 0.9)
    store.save()
    fresh = BeliefStore(store.dir_path)
    fresh.load()
    assert fresh.conf(("cpu", "load", "high")) == pytest.approx(0.9)


# -- Organism.sense() ------------------------------------------------------

def test_sense_folds_metrics_into_store(proc, sys_tree):
    org = Organism(proc.parent,
                   probe=_probe(proc, sys_tree, ncpu=4, clock=_utc_midnight))
    org.load()
    org.sense()
    b = org.store.beliefs()
    assert ("cpu", "load", "low") in b
    assert ("mem", "usage", "mid") in b
    assert ("temp", "cpu", "warm") in b
    assert ("system", "uptime", "day") in b
    assert ("time", "hour", "midnight") in b


def test_sense_bumps_stress_on_adverse_system(proc, sys_tree):
    _write(proc / "loadavg", "20.00 20.00 20.00 2/100 1000\n")
    _write(proc.parent / "sys/class/thermal/thermal_zone0/temp", "99000\n")
    _write(proc.parent / "sys/class/power_supply/BAT1/capacity", "5\n")
    org = Organism(proc.parent, probe=_probe(proc, sys_tree, ncpu=4,
                                             statvfs=_disk(1000, 10)))
    org.load()
    org.sense()
    assert org.store.stress > org.meter.BASELINE


def test_sense_does_not_stack_stress_on_persistent_adverse(proc, sys_tree):
    """A persistently loaded host must not drive the organism toward the
    fade zone (regression: per-second distress + slow decay pinned stress
    at 1.0 and killed it after 3 transitions)."""
    _write(proc / "loadavg", "20.00 20.00 20.00 2/100 1000\n")
    _write(proc.parent / "sys/class/thermal/thermal_zone0/temp", "99000\n")
    _write(proc.parent / "sys/class/power_supply/BAT1/capacity", "5\n")
    org = Organism(proc.parent, probe=_probe(proc, sys_tree, ncpu=4,
                                             statvfs=_disk(1000, 10)))
    org.load()
    for _ in range(20):
        org.sense()
    assert org.store.stress < 0.5  # far below Lifecycle.FADE_STRESS


def test_sense_replaces_stale_metric_readings(proc, sys_tree):
    org = Organism(proc.parent, probe=_probe(proc, sys_tree, ncpu=4))
    org.load()
    org.sense()
    _write(proc / "loadavg", "9.00 9.00 9.00 2/100 1000\n")
    org.sense()
    assert ("cpu", "load", "high") in org.store.beliefs()
    assert ("cpu", "load", "low") not in org.store.beliefs()


# -- migration away from the toy object world ------------------------------

def test_load_migrates_legacy_object_beliefs(store):
    store.add(("apple", "color", "red"), 0.9)
    store.add(("ball", "shape", "round"), 0.9)
    store.add(("self", "mood", "calm"), 0.9)
    store.save()
    org = Organism(store.dir_path, probe=_probe(Path("/nonexistent/proc"),
                                                Path("/nonexistent/sys")))
    org.load()
    beliefs = org.store.beliefs()
    assert ("self", "mood", "calm") in beliefs
    assert not any(obj in ("apple", "ball", "milk", "water")
                   for (obj, _a, _v) in beliefs)


def test_fresh_boot_seeds_self_core_not_objects(tmp_path):
    shutil.copy(Path(__file__).parent.parent / "organism.scl", tmp_path / "organism.scl")
    org = Organism(tmp_path, probe=_probe(Path("/nonexistent/proc"),
                                          Path("/nonexistent/sys")))
    org.load()
    assert ("self", "mood", "calm") in org.store.beliefs()
    assert not any(obj in ("apple", "ball", "milk", "water")
                   for (obj, _a, _v) in org.store.beliefs())


# -- helper ----------------------------------------------------------------

def probe_beliefs(proc, sys_tree):
    return _probe(proc, sys_tree, ncpu=4).beliefs(
        _probe(proc, sys_tree, ncpu=4).snapshot())


@pytest.fixture
def store(tmp_path):
    return BeliefStore(tmp_path)
