"""System probe: the organism's senses. Samples the host machine from the
same sources btm reads — /proc/stat (CPU), /proc/meminfo (memory),
/proc/loadavg (load), /proc/uptime, /sys/class/thermal (temperatures),
/sys/class/power_supply (battery) and statvfs (disk) — plus the UTC wall
clock, and quantizes the continuous metrics into discrete symbolic
beliefs the reasoner can use (e.g. cpu:load=high, mem:usage=mid,
temp:cpu=hot, time:hour=fourteen)."""

import os
from datetime import datetime, timezone
from pathlib import Path

LOAD_LOW = 0.5      # load1/ncpu below this -> "low"
LOAD_MID = 1.0      # load1/ncpu below this -> "mid", else "high"
MEM_LOW = 50.0      # percent used below this -> "low"
MEM_MID = 80.0      # percent used below this -> "mid", else "high"
DISK_LOW = 10.0     # percent free below this -> "low"
DISK_MID = 30.0     # percent free below this -> "mid", else "ok"
TEMP_COOL = 50.0    # celsius below this -> "cool"
TEMP_WARM = 80.0    # celsius below this -> "warm", else "hot"
BATTERY_LOW = 20.0  # percent below this -> "low", else "ok"
UPTIME_BRIEF = 3600.0    # seconds below this -> "brief"
UPTIME_DAY = 86400.0     # seconds below this -> "day", else "long"

# hour of the UTC clock as a symbolic value (VALID_VALUE_RE: letters only)
HOUR_WORDS = (
    "midnight", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty", "twenty_one", "twenty_two",
    "twenty_three",
)

# adverse-metric stress weights, capped by DISTRESS_CAP
DISTRESS_WEIGHTS = {
    ("cpu", "load", "high"): 0.04,
    ("mem", "usage", "high"): 0.03,
    ("disk", "space", "low"): 0.04,
    ("temp", "cpu", "hot"): 0.03,
    ("battery", "level", "low"): 0.02,
}
DISTRESS_CAP = 0.15


def _read_float(path):
    """First whitespace-separated field of a file, or None."""
    try:
        return float(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


class SystemProbe:
    """Reads live system metrics. All sources are injectable so tests can
    point at fake /proc and /sys trees."""

    def __init__(self, proc="/proc", sys="/sys", ncpu=None, statvfs=None,
                 clock=None):
        self.proc = Path(proc)
        self.sys = Path(sys)
        self.ncpu = ncpu if ncpu is not None else os.cpu_count() or 1
        self._statvfs = statvfs or os.statvfs
        self._clock = clock if clock is not None \
            else (lambda: datetime.now(timezone.utc))
        self._prev_cpu = None   # (idle, total) from the previous stat read
        self._adverse_seen = set()   # adverse beliefs already counted

    def _clock_now(self):
        """The wall clock, always normalized to UTC (naive clocks are
        assumed to already be UTC)."""
        now = self._clock()
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def clock_utc(self):
        """Computer time as 'HH:MM UTC' (e.g. '14:30 UTC')."""
        now = self._clock_now()
        return f"{now.hour:02d}:{now.minute:02d} UTC"

    # -- raw metrics ------------------------------------------------------
    def snapshot(self):
        now = self._clock_now()
        return {
            "cpu_percent": self._cpu_percent(),
            "mem_percent": self._mem_percent(),
            "load_ratio": self._load_ratio(),
            "uptime_s": _read_float(self.proc / "uptime"),
            "temp_c": self._max_temp_c(),
            "battery_percent": self._battery_percent(),
            "battery_charging": self._battery_charging(),
            "disk_free_percent": self._disk_free_percent(),
            "clock_hour": now.hour,
            "clock_minute": now.minute,
        }

    def _cpu_percent(self):
        """CPU busy% from a /proc/stat delta. First read returns None
        (warms the baseline); later reads are deltas since the last read."""
        try:
            line = (self.proc / "stat").read_text().splitlines()[0]
            fields = [int(v) for v in line.split()[1:]]
            idle = fields[3] + fields[4]          # idle + iowait
            total = sum(fields)
        except (OSError, ValueError, IndexError):
            return None
        if self._prev_cpu is None:
            self._prev_cpu = (idle, total)
            return None
        prev_idle, prev_total = self._prev_cpu
        self._prev_cpu = (idle, total)
        idle_delta = idle - prev_idle
        total_delta = total - prev_total
        if total_delta <= 0:
            return 0.0
        return 100.0 * (1.0 - idle_delta / total_delta)

    def _mem_percent(self):
        try:
            info = (self.proc / "meminfo").read_text()
            total = int(next(l for l in info.splitlines()
                             if l.startswith("MemTotal:")).split()[1])
            avail = int(next(l for l in info.splitlines()
                             if l.startswith("MemAvailable:")).split()[1])
        except (OSError, ValueError, IndexError, StopIteration):
            return None
        if total <= 0:
            return None
        return 100.0 * (total - avail) / total

    def _load_ratio(self):
        load1 = _read_float(self.proc / "loadavg")
        return load1 / self.ncpu if load1 is not None else None

    def _max_temp_c(self):
        temps = []
        try:
            zones = sorted(self.sys.glob("class/thermal/thermal_zone*/temp"))
        except OSError:
            zones = []
        for zone in zones:
            t = _read_float(zone)
            if t is not None:
                temps.append(t / 1000.0)  # millidegrees -> celsius
        return max(temps) if temps else None

    def _battery_percent(self):
        capacities = []
        try:
            caps = sorted(self.sys.glob("class/power_supply/BAT*/capacity"))
        except OSError:
            caps = []
        for cap in caps:
            c = _read_float(cap)
            if c is not None:
                capacities.append(c)
        return int(max(capacities)) if capacities else None

    def _battery_charging(self):
        try:
            stats = sorted(self.sys.glob("class/power_supply/BAT*/status"))
        except OSError:
            stats = []
        for status in stats:
            try:
                text = status.read_text().strip().lower()
            except OSError:
                continue
            if text:
                return "charging" in text or "full" in text
        return None

    def _disk_free_percent(self):
        try:
            st = self._statvfs("/")
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
        except (OSError, ZeroDivisionError, AttributeError):
            return None
        if total <= 0:
            return None
        return 100.0 * free / total

    # -- quantization ------------------------------------------------------
    def beliefs(self, snap):
        """Continuous metrics -> discrete (obj, attr, val) beliefs."""
        b = {}
        if snap["load_ratio"] is not None:
            lvl = ("low" if snap["load_ratio"] < LOAD_LOW
                   else "mid" if snap["load_ratio"] < LOAD_MID else "high")
            b[("cpu", "load", lvl)] = 0.9
        if snap["mem_percent"] is not None:
            lvl = ("low" if snap["mem_percent"] < MEM_LOW
                   else "mid" if snap["mem_percent"] < MEM_MID else "high")
            b[("mem", "usage", lvl)] = 0.9
        if snap["disk_free_percent"] is not None:
            lvl = ("low" if snap["disk_free_percent"] < DISK_LOW
                   else "mid" if snap["disk_free_percent"] < DISK_MID else "ok")
            b[("disk", "space", lvl)] = 0.9
        if snap["temp_c"] is not None:
            lvl = ("cool" if snap["temp_c"] < TEMP_COOL
                   else "warm" if snap["temp_c"] < TEMP_WARM else "hot")
            b[("temp", "cpu", lvl)] = 0.9
        if snap["battery_percent"] is not None:
            lvl = ("low" if snap["battery_percent"] < BATTERY_LOW else "ok")
            b[("battery", "level", lvl)] = 0.9
            if snap["battery_charging"] is not None:
                b[("battery", "charging",
                   "true" if snap["battery_charging"] else "false")] = 0.9
        if snap["uptime_s"] is not None:
            lvl = ("brief" if snap["uptime_s"] < UPTIME_BRIEF
                   else "day" if snap["uptime_s"] < UPTIME_DAY else "long")
            b[("system", "uptime", lvl)] = 0.9
        b[("time", "hour", HOUR_WORDS[snap["clock_hour"]])] = 0.9
        return b

    # -- stress coupling ---------------------------------------------------
    def distress(self, snap):
        """Adverse-metric pressure, edge-triggered: only conditions that
        newly appear (cross into adverse territory) count, each once, and
        each capped by DISTRESS_CAP. A persistently busy host therefore
        bumps stress on the first sense() but does not re-stack every
        second, so ambient load alone can never pin stress in the fade
        zone. If an adverse condition recovers and later returns, it
        counts again."""
        current = {b for b in DISTRESS_WEIGHTS if self.beliefs(snap).get(b)}
        amount = sum(DISTRESS_WEIGHTS[b] for b in current - self._adverse_seen)
        self._adverse_seen = current
        return min(amount, DISTRESS_CAP)
