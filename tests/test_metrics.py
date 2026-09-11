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


# --- trend --------------------------------------------------------------

def test_no_sparkline_from_a_single_reading():
    """One point implies a trend that has not actually been observed."""
    assert metrics.spark([]) is None
    assert metrics.spark([50]) is None


def test_a_sparkline_has_a_point_per_reading():
    path = metrics.spark([10, 40, 25, 90])
    assert path["line"].count("L") == 3          # first point is the M
    assert path["line"].startswith("M2.0,"), "starts at the inset, not the edge"


def test_a_sparkline_stays_inside_its_box():
    """Inset on every side: the stroke and end marker must not paint over the
    edge of the tile they sit in."""
    path = metrics.spark([1, 2, 3], width=88, height=26)
    xs = [float(part.split(",")[0]) for part in path["line"].replace("M", "").split(" L")]
    assert min(xs) >= 1.5
    assert max(xs) <= 86.5


def test_high_values_sit_above_low_ones():
    """SVG y grows downwards, so a larger reading must have a smaller y."""
    low = metrics.spark([0, 0])["line"]
    high = metrics.spark([100, 100])["line"]
    assert float(high.split(",")[1].split(" ")[0]) < float(low.split(",")[1].split(" ")[0])


def test_flat_extremes_stay_inside_the_box():
    for values in ([0, 0], [100, 100]):
        path = metrics.spark(values, height=26)
        ys = [float(part.split(",")[1]) for part in path["line"].replace("M", "").split(" L")]
        assert all(0 <= y <= 26 for y in ys)


def test_readings_outside_the_range_are_clamped():
    path = metrics.spark([-20, 140], floor=0, ceiling=100, height=26)
    ys = [float(part.split(",")[1]) for part in path["line"].replace("M", "").split(" L")]
    assert all(0 <= y <= 26 for y in ys)


def test_the_area_closes_back_to_the_baseline():
    path = metrics.spark([10, 20, 30])
    assert path["area"].endswith("Z")


def test_sampling_is_rate_limited(monkeypatch):
    """The dashboard polls every few seconds; the series should not race it."""
    metrics._history.clear()
    monkeypatch.setattr(metrics, "_last_sample", 0.0)

    metrics.sample()
    first = len(metrics._history)
    metrics.sample()
    metrics.sample()

    assert len(metrics._history) == first == 1


def test_the_series_is_capped():
    metrics._history.clear()
    for value in range(metrics.HISTORY_POINTS * 2):
        metrics._history.append((float(value), float(value)))
    assert len(metrics._history) == metrics.HISTORY_POINTS


# --- uptime -------------------------------------------------------------

def test_uptime_of_this_process_is_positive():
    assert metrics.uptime_seconds(os.getpid()) > 0


def test_uptime_of_nothing_is_zero():
    assert metrics.uptime_seconds(None) == 0.0
    assert metrics.uptime_seconds(0) == 0.0


@pytest.mark.parametrize("seconds,expected", [
    (12, "12s"), (300, "5m"), (7200, "2h"), (400000, "4d"),
])
def test_durations_read_naturally(seconds, expected):
    assert metrics.human_duration(seconds) == expected


def test_a_sparkline_carries_the_box_it_was_drawn_for():
    """The template renders this viewBox. When the two were stated separately
    they drifted, and the line was laid out for a bigger box and clipped away."""
    path = metrics.spark([10, 50, 30])

    assert path["width"] == str(metrics.SPARK_WIDTH)
    assert path["height"] == str(metrics.SPARK_HEIGHT)

    xs, ys = [], []
    for part in path["line"].replace("M", "").split(" L"):
        x, y = part.split(",")
        xs.append(float(x))
        ys.append(float(y))
    assert max(xs) <= metrics.SPARK_WIDTH
    assert max(ys) <= metrics.SPARK_HEIGHT, "drawn below the box is drawn invisibly"
