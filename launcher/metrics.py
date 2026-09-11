"""Resource usage for the dashboard: per-app and for the machine as a whole.

Two things make this less trivial than calling psutil:

* CPU percentage is measured *between calls* on the same process object, so the
  objects are cached rather than recreated - a fresh psutil.Process always
  reports 0.0 the first time it is asked.
* Disk usage means walking an app's whole tree, including a virtual
  environment with thousands of files. That is far too slow to do on every
  dashboard poll, so it is cached with a short lifetime.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import psutil

from . import config

# Percentages at or above these read as warning / critical in the UI.
WARNING_PERCENT = 70.0
CRITICAL_PERCENT = 90.0

_DISK_CACHE_SECONDS = 120

# A short rolling history, so the dashboard can show whether a number is
# climbing rather than only what it is right now. Held in memory: it is
# context for a glance, not a record worth keeping across restarts.
HISTORY_POINTS = 40
SAMPLE_EVERY_SECONDS = 10

# The sparkline's own geometry. Stated once and carried in the result, so the
# drawing and the box it is drawn into cannot disagree - they did, and the
# line was laid out for a larger box and clipped out of sight entirely.
SPARK_WIDTH = 62
SPARK_HEIGHT = 22

_history: deque[tuple[float, float]] = deque(maxlen=HISTORY_POINTS)
_last_sample = 0.0

_process_cache: dict[int, psutil.Process] = {}
_disk_cache: dict[str, tuple[float, int]] = {}
_lock = threading.Lock()

# psutil reports system CPU since the previous call, so prime it at import.
psutil.cpu_percent(interval=None)


@dataclass
class Usage:
    cpu_percent: float      # share of the whole machine, not of one core
    memory_mb: float
    disk_bytes: int


def _process(pid: int) -> psutil.Process | None:
    """A cached process handle, so cpu_percent has something to compare against."""
    with _lock:
        proc = _process_cache.get(pid)
        if proc is not None and proc.is_running():
            return proc
        try:
            proc = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            _process_cache.pop(pid, None)
            return None
        _process_cache[pid] = proc
        # First reading after adopting a process is always 0.0; take it now so
        # the next call has an interval to measure against.
        try:
            proc.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        return proc


def _tree(pid: int | None) -> list[psutil.Process]:
    if not pid:
        return []
    proc = _process(pid)
    if proc is None:
        return []
    try:
        return [proc, *proc.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return [proc]


def _cpu_and_memory(pids: list[int | None]) -> tuple[float, float]:
    cores = psutil.cpu_count() or 1
    cpu = 0.0
    memory = 0.0
    for pid in pids:
        for proc in _tree(pid):
            try:
                cpu += proc.cpu_percent(interval=None)
                memory += proc.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    # psutil counts one saturated core as 100%, so divide by the core count to
    # express this as a share of the machine - which is what a meter against
    # 100% has to mean.
    return min(cpu / cores, 100.0), memory / 1024 / 1024


def _directory_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def disk_bytes(app_name: str) -> int:
    """Everything this app occupies: its source, environment, uploads and data."""
    now = time.time()
    cached = _disk_cache.get(app_name)
    if cached and now - cached[0] < _DISK_CACHE_SECONDS:
        return cached[1]

    total = sum(
        _directory_size(directory / app_name)
        for directory in (config.SRC_DIR, config.UPLOAD_DIR, config.APPDATA_DIR)
    )
    _disk_cache[app_name] = (now, total)
    return total


def forget(app_name: str) -> None:
    _disk_cache.pop(app_name, None)


def for_app(row) -> Usage:
    """Usage for one app. Zeros for anything not currently running."""
    if row["status"] != "live":
        return Usage(0.0, 0.0, disk_bytes(row["name"]))
    cpu, memory = _cpu_and_memory([row["front_pid"], row["pid"]])
    return Usage(cpu, memory, disk_bytes(row["name"]))


def sample() -> None:
    """Record one point of host CPU and memory, at most every few seconds.

    Driven by whoever asks - a dashboard poll or the supervisor's own loop -
    so the series keeps filling whether or not anyone is watching.
    """
    global _last_sample
    now = time.time()
    with _lock:
        if now - _last_sample < SAMPLE_EVERY_SECONDS:
            return
        _last_sample = now
    _history.append((psutil.cpu_percent(interval=None), psutil.virtual_memory().percent))


def history() -> tuple[list[float], list[float]]:
    """The recorded CPU and memory series, oldest first."""
    points = list(_history)
    return [p[0] for p in points], [p[1] for p in points]


def spark(values: list[float], width: float = SPARK_WIDTH,
          height: float = SPARK_HEIGHT,
          floor: float = 0.0, ceiling: float = 100.0) -> dict[str, str] | None:
    """Turn a series into an SVG path pair - the line, and the area under it.

    Returns None below two points: a sparkline drawn from one reading implies
    a trend that has not been observed.
    """
    if len(values) < 2:
        return None

    span = max(ceiling - floor, 1e-9)
    # Inset on every side so the stroke and the marker at the end sit wholly
    # inside the box. Without it they paint over the edge of the tile.
    pad = 2.0
    drawable = max(width - pad * 2, 1e-9)
    step = drawable / (len(values) - 1)
    usable = height - pad * 2

    points = [
        (
            round(pad + index * step, 2),
            round(height - pad - (min(max(value, floor), ceiling) - floor) / span * usable, 2),
        )
        for index, value in enumerate(values)
    ]
    line = "M" + " L".join(f"{x},{y}" for x, y in points)
    area = f"{line} L{points[-1][0]},{height} L{points[0][0]},{height} Z"
    return {
        "line": line,
        "area": area,
        "last_x": str(points[-1][0]),
        "last_y": str(points[-1][1]),
        # Carried with the paths so the template renders the box these points
        # were actually laid out for.
        "width": str(width),
        "height": str(height),
    }


def uptime_seconds(pid: int | None) -> float:
    """How long an app's front process has been up, from the process itself."""
    if not pid:
        return 0.0
    proc = _process(pid)
    if proc is None:
        return 0.0
    try:
        return max(0.0, time.time() - proc.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0.0


def human_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def host() -> dict:
    """What the machine as a whole is doing."""
    sample()
    cpu_series, memory_series = history()
    memory = psutil.virtual_memory()
    config.ensure_dirs()
    disk = psutil.disk_usage(str(config.DATA_DIR))
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_cores": psutil.cpu_count() or 1,
        "memory_used_gb": (memory.total - memory.available) / 1024**3,
        "memory_total_gb": memory.total / 1024**3,
        "memory_percent": memory.percent,
        "disk_used_gb": disk.used / 1024**3,
        "disk_total_gb": disk.total / 1024**3,
        "disk_free_gb": disk.free / 1024**3,
        "disk_percent": disk.percent,
        "cpu_spark": spark(cpu_series),
        "memory_spark": spark(memory_series),
    }


def state_for(percent: float) -> str:
    """Severity band a meter fill should use. Never the only signal - the
    number is always displayed beside it."""
    if percent >= CRITICAL_PERCENT:
        return "critical"
    if percent >= WARNING_PERCENT:
        return "warning"
    return "ok"


def human_bytes(num_bytes: float) -> str:
    if num_bytes <= 0:
        return "0 MB"
    for unit in ("KB", "MB", "GB"):
        num_bytes /= 1024
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "KB" else f"{num_bytes:.1f} {unit}"
    return f"{num_bytes:.1f} GB"
