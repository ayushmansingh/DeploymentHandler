"""Resource numbers behind the dashboard's meters."""
import os

import pytest

from launcher import config, db, metrics


def test_host_reports_usable_numbers(data_dir):
    host = metrics.host()

    assert 0 <= host["cpu_percent"] <= 100
    assert host["cpu_cores"] >= 1
    assert 0 < host["memory_used_gb"] < host["memory_total_gb"]
    assert 0 <= host["memory_percent"] <= 100
    assert host["disk_free_gb"] > 0


@pytest.mark.parametrize("percent,expected", [
    (0, "ok"), (69.9, "ok"),
    (70, "warning"), (89.9, "warning"),
    (90, "critical"), (100, "critical"),
])
def test_severity_bands(percent, expected):
    assert metrics.state_for(percent) == expected


def test_running_process_reports_cpu_and_memory(data_dir):
    """Measured against this test process, which certainly exists."""
    db.init()
    app_id = db.create_app("alpha")
    db.update_app(app_id, status="live", front_pid=os.getpid(), pid=None)

    usage = metrics.for_app(db.get_app(app_id))

    assert usage.memory_mb > 1, "a live Python process uses more than a megabyte"
    assert usage.cpu_percent >= 0


def test_stopped_app_reports_no_cpu_or_memory(data_dir):
    db.init()
    app_id = db.create_app("alpha")
    db.update_app(app_id, status="stopped", front_pid=os.getpid())

    usage = metrics.for_app(db.get_app(app_id))

    assert usage.cpu_percent == 0
    assert usage.memory_mb == 0


def test_disk_covers_source_uploads_and_saved_data(data_dir):
    for directory in (config.SRC_DIR, config.UPLOAD_DIR, config.APPDATA_DIR):
        (directory / "alpha").mkdir(parents=True, exist_ok=True)
        (directory / "alpha" / "file.bin").write_bytes(b"x" * 1000)

    metrics.forget("alpha")
    assert metrics.disk_bytes("alpha") == 3000


def test_disk_is_cached_because_walking_a_venv_is_slow(data_dir):
    (config.SRC_DIR / "alpha").mkdir(parents=True, exist_ok=True)
    (config.SRC_DIR / "alpha" / "a.bin").write_bytes(b"x" * 500)
    metrics.forget("alpha")
    first = metrics.disk_bytes("alpha")

    (config.SRC_DIR / "alpha" / "b.bin").write_bytes(b"x" * 500)
    assert metrics.disk_bytes("alpha") == first, "should serve the cached value"

    metrics.forget("alpha")
    assert metrics.disk_bytes("alpha") == 1000


def test_cpu_is_a_share_of_the_whole_machine(data_dir, monkeypatch):
    """psutil counts one saturated core as 100%; a meter needs 0-100 overall."""
    db.init()
    app_id = db.create_app("alpha")
    db.update_app(app_id, status="live", front_pid=os.getpid())

    monkeypatch.setattr(metrics.psutil, "cpu_count", lambda: 4)
    usage = metrics.for_app(db.get_app(app_id))
    assert usage.cpu_percent <= 100


def test_human_bytes_reads_naturally():
    assert metrics.human_bytes(0) == "0 MB"
    assert metrics.human_bytes(2048) == "2 KB"
    assert metrics.human_bytes(5 * 1024**3) == "5.0 GB"
