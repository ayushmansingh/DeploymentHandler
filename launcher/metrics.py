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
from dataclasses import dataclass
from pathlib import Path

import psutil

from . import config

# Percentages at or above these read as warning / critical in the UI.
WARNING_PERCENT = 70.0
CRITICAL_PERCENT = 90.0

_DISK_CACHE_SECONDS = 120

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


def host() -> dict:
    """What the machine as a whole is doing."""
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
