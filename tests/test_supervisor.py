"""The supervisor: what replaces Docker's restart policy in native mode."""
import pytest

from launcher import config, db, native, supervisor


@pytest.fixture
def live_app(data_dir, monkeypatch):
    db.init()
    monkeypatch.setattr(config, "RUNTIME", "native")
    app_id = db.create_app("alpha")
    db.update_app(
        app_id, status="live", host_port=24817, backend_port=30412,
        pid=1001, front_pid=1002,
    )
    # A real project directory, so launch() gets past its existence check.
    src = config.SRC_DIR / "alpha"
    (src / "backend").mkdir(parents=True)
    (src / "backend" / "requirements.txt").write_text("fastapi\n")
    (src / "backend" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    return db.get_app(app_id)


@pytest.fixture
def relaunches(monkeypatch):
    """Record launches instead of starting real processes."""
    calls: list[str] = []
    monkeypatch.setattr(native, "stop", lambda procs, **markers: None)
    monkeypatch.setattr(
        native, "start",
        lambda *a, **k: (calls.append(a[0]) or native.Processes(front_pid=2002, backend_pid=2001)),
    )
    return calls


def alive(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(native, "is_running", lambda pid, marker=None: value)


def memory(monkeypatch, mb: float) -> None:
    monkeypatch.setattr(native, "memory_mb", lambda pid: mb / 2)


def test_dead_app_is_restarted(live_app, relaunches, monkeypatch):
    alive(monkeypatch, False)
    memory(monkeypatch, 0)

    supervisor.check_once()

    assert relaunches == ["alpha"]
    assert db.get_app_by_name("alpha")["front_pid"] == 2002


def test_healthy_app_is_left_alone(live_app, relaunches, monkeypatch):
    alive(monkeypatch, True)
    memory(monkeypatch, 100)

    supervisor.check_once()

    assert relaunches == []


def test_stopped_app_is_not_resurrected(live_app, relaunches, monkeypatch):
    """A deliberate stop must stick, or the Stop button does nothing."""
    db.update_app(int(live_app["id"]), status="stopped")
    alive(monkeypatch, False)
    memory(monkeypatch, 0)

    supervisor.check_once()

    assert relaunches == []


def test_memory_spike_alone_does_not_bounce_an_app(live_app, relaunches, monkeypatch):
    alive(monkeypatch, True)
    memory(monkeypatch, config.APP_MEMORY_LIMIT_MB * 2)

    supervisor.check_once()

    assert relaunches == [], "one spike should not restart a working app"


def test_sustained_memory_overuse_restarts_the_app(live_app, relaunches, monkeypatch):
    alive(monkeypatch, True)
    memory(monkeypatch, config.APP_MEMORY_LIMIT_MB * 2)

    for _ in range(config.MEMORY_STRIKES_BEFORE_RESTART):
        supervisor.check_once()

    assert relaunches == ["alpha"]


def test_recovery_clears_the_strike_count(live_app, relaunches, monkeypatch):
    alive(monkeypatch, True)
    memory(monkeypatch, config.APP_MEMORY_LIMIT_MB * 2)
    supervisor.check_once()

    memory(monkeypatch, 10)
    supervisor.check_once()

    memory(monkeypatch, config.APP_MEMORY_LIMIT_MB * 2)
    supervisor.check_once()

    assert relaunches == [], "strikes must not accumulate across a recovery"


def test_launch_reports_missing_files_instead_of_crashing(live_app, relaunches, monkeypatch):
    import shutil
    shutil.rmtree(config.SRC_DIR / "alpha")

    assert supervisor.launch(live_app) is False
    assert db.get_app_by_name("alpha")["status"] == "failed"
    assert relaunches == []


def test_stop_clears_recorded_pids(live_app, monkeypatch):
    stopped: list = []
    monkeypatch.setattr(
        native, "stop",
        lambda procs, **markers: stopped.append((procs, markers)),
    )

    supervisor.stop(live_app)

    procs, markers = stopped[0]
    assert procs.front_pid == 1002 and procs.backend_pid == 1001
    after = db.get_app_by_name("alpha")
    assert after["pid"] is None and after["front_pid"] is None


def test_stop_passes_the_markers_the_health_checks_use(live_app, monkeypatch):
    """Without them a reused pid gets killed - see native._kill_tree.

    They are asserted here rather than in native because the bug was a
    caller that had the markers and did not pass them, not a kill that
    ignored one it was given.
    """
    stopped: list = []
    monkeypatch.setattr(
        native, "stop",
        lambda procs, **markers: stopped.append((procs, markers)),
    )

    supervisor.stop(live_app)

    _, markers = stopped[0]
    assert markers["front_marker"] == "--port 24817"
    assert markers["backend_marker"] == "30412"


def test_restore_starts_apps_that_did_not_survive_a_reboot(live_app, relaunches, monkeypatch):
    alive(monkeypatch, False)

    supervisor.restore_on_startup()

    assert relaunches == ["alpha"]


def test_restore_leaves_survivors_running(live_app, relaunches, monkeypatch):
    alive(monkeypatch, True)

    supervisor.restore_on_startup()

    assert relaunches == []


def test_build_interrupted_by_a_restart_is_resolved(live_app, monkeypatch):
    """Nothing will finish it, so it must not sit on the dashboard forever."""
    db.update_app(int(live_app["id"]), status="building")
    deploy_id = db.create_deploy(int(live_app["id"]), "/tmp/x.zip")
    db.set_deploy_status(deploy_id, "building")
    alive(monkeypatch, False)

    supervisor.recover_interrupted_deploys()

    deploy = db.get_deploy(deploy_id)
    assert deploy["status"] == "failed"
    assert "restarted while this was building" in deploy["error_summary"]
    assert db.get_app_by_name("alpha")["status"] == "failed"


def test_interrupted_build_does_not_take_down_the_running_version(live_app, monkeypatch):
    """A replace that never finished leaves the previous version serving."""
    db.update_app(int(live_app["id"]), status="building")
    deploy_id = db.create_deploy(int(live_app["id"]), "/tmp/x.zip")
    db.set_deploy_status(deploy_id, "building")
    alive(monkeypatch, True)

    supervisor.recover_interrupted_deploys()

    assert db.get_deploy(deploy_id)["status"] == "failed"
    assert db.get_app_by_name("alpha")["status"] == "live"


def test_queued_deploys_are_resolved_too(live_app, monkeypatch):
    deploy_id = db.create_deploy(int(live_app["id"]), "/tmp/x.zip")
    alive(monkeypatch, True)

    supervisor.recover_interrupted_deploys()

    assert db.get_deploy(deploy_id)["status"] == "failed"


def test_finished_deploys_are_left_alone(live_app, monkeypatch):
    deploy_id = db.create_deploy(int(live_app["id"]), "/tmp/x.zip")
    db.finish_deploy(deploy_id, "live")
    alive(monkeypatch, True)

    supervisor.recover_interrupted_deploys()

    assert db.get_deploy(deploy_id)["status"] == "live"


def test_restore_resolves_interrupted_builds_as_well(live_app, relaunches, monkeypatch):
    deploy_id = db.create_deploy(int(live_app["id"]), "/tmp/x.zip")
    db.set_deploy_status(deploy_id, "building")
    alive(monkeypatch, False)

    supervisor.restore_on_startup()

    assert db.get_deploy(deploy_id)["status"] == "failed"
    assert relaunches == ["alpha"], "the live app is still brought back"


def test_an_app_that_dies_on_start_is_not_restarted_for_ever(data_dir, monkeypatch):
    """A crash at import repeats identically however many times it is retried.
    Restarting on a timer spawned a process every few seconds and wrote the
    same traceback to the log indefinitely - and left the dashboard claiming
    the app was live the whole time, because launch() reports success as soon
    as it has spawned something."""
    from launcher import config, db, supervisor

    db.init()
    app_id = db.create_app("query-allocation-dashboard")
    db.update_app(app_id, status="live", host_port=24817, backend_port=31000,
                  front_pid=111, pid=222)

    launches = []
    monkeypatch.setattr(supervisor, "launch",
                        lambda row, reason="": launches.append(row["name"]) or True)
    monkeypatch.setattr(supervisor, "stop", lambda row: None)
    # Never alive, exactly like a backend that raises on import.
    monkeypatch.setattr(supervisor, "_front_alive", lambda row: False)
    monkeypatch.setattr(supervisor, "_backend_alive", lambda row: False)
    supervisor._restarts.clear()

    for _ in range(10):
        supervisor.check_once()

    limit = config.RESTARTS_BEFORE_GIVING_UP
    assert len(launches) == limit, f"should stop after {limit}, not keep going"
    assert db.get_app(app_id)["status"] == "failed", (
        "a broken app must stop claiming to be live"
    )


def test_a_restart_that_sticks_clears_the_count(data_dir, monkeypatch):
    """One crash followed by a healthy restart must not leave the app one
    strike from being given up on days later."""
    from launcher import config, db, supervisor

    db.init()
    app_id = db.create_app("sales-dashboard")
    db.update_app(app_id, status="live", host_port=24817, backend_port=31000,
                  front_pid=111, pid=222)

    alive = {"value": False}
    monkeypatch.setattr(supervisor, "launch", lambda row, reason="": True)
    monkeypatch.setattr(supervisor, "stop", lambda row: None)
    monkeypatch.setattr(supervisor, "_front_alive", lambda row: alive["value"])
    monkeypatch.setattr(supervisor, "_backend_alive", lambda row: alive["value"])
    monkeypatch.setattr(supervisor.native, "memory_mb", lambda pid: 10.0)
    supervisor._restarts.clear()

    supervisor.check_once()                     # dies once
    assert supervisor._restarts.get(app_id) == 1

    alive["value"] = True
    supervisor.check_once()                     # comes back and stays
    assert app_id not in supervisor._restarts, "a healthy check clears the count"
    assert db.get_app(app_id)["status"] == "live"


def test_apps_that_were_already_running_get_their_picture_filled_in(data_dir, monkeypatch):
    """Capture happens on deploy, so every app already running when this
    arrived would have sat on a generated tile until somebody redeployed them
    one at a time."""
    from launcher import db, supervisor, thumbs

    db.init()
    for name in ("alpha", "beta"):
        app_id = db.create_app(name)
        db.update_app(app_id, status="live", host_port=24000 + app_id,
                      backend_port=31000 + app_id, front_pid=1, pid=2)

    taken: list[str] = []
    monkeypatch.setattr(thumbs, "capture",
                        lambda name, url: taken.append(name) or True)
    monkeypatch.setattr(thumbs, "has_thumb", lambda name: name in taken)
    monkeypatch.setattr(supervisor, "_front_alive", lambda row: True)
    monkeypatch.setattr(supervisor, "_backend_alive", lambda row: True)
    monkeypatch.setattr(supervisor.native, "memory_mb", lambda pid: 10.0)
    supervisor._pictured.clear()

    # One per pass, so a dozen browsers never start at once.
    supervisor.check_once()
    _join_picture_threads()
    assert len(taken) == 1

    supervisor.check_once()
    _join_picture_threads()
    assert sorted(taken) == ["alpha", "beta"]

    # And once everything has one, it stops asking.
    supervisor.check_once()
    _join_picture_threads()
    assert len(taken) == 2


def test_an_app_whose_picture_fails_is_not_retried_every_pass(data_dir, monkeypatch):
    """A browser that cannot photograph one app will not manage it fifteen
    seconds later either."""
    from launcher import db, supervisor, thumbs

    db.init()
    app_id = db.create_app("stubborn")
    db.update_app(app_id, status="live", host_port=24817, backend_port=31000,
                  front_pid=1, pid=2)

    attempts: list[str] = []
    monkeypatch.setattr(thumbs, "capture",
                        lambda name, url: attempts.append(name) or False)
    monkeypatch.setattr(thumbs, "has_thumb", lambda name: False)
    monkeypatch.setattr(supervisor, "_front_alive", lambda row: True)
    monkeypatch.setattr(supervisor, "_backend_alive", lambda row: True)
    monkeypatch.setattr(supervisor.native, "memory_mb", lambda pid: 10.0)
    supervisor._pictured.clear()

    for _ in range(5):
        supervisor.check_once()
        _join_picture_threads()

    assert attempts == ["stubborn"], "tried once, not on every pass forever"


def _join_picture_threads() -> None:
    import threading
    for thread in threading.enumerate():
        if thread.name.startswith("picture-"):
            thread.join(timeout=5)


def test_one_app_that_cannot_be_started_does_not_stop_the_launcher(live_app, monkeypatch):
    """This is the bug that took the server down, not just one dashboard card.

    An old pid that the machine refused to let us terminate raised
    psutil.AccessDenied out of launch(); nothing caught it, so it reached
    uvicorn's startup and the launcher exited instead of serving.
    """
    import psutil

    db.create_app("beta")
    beta = db.get_app_by_name("beta")
    db.update_app(int(beta["id"]), status="live", host_port=24818,
                  backend_port=30413, pid=2001, front_pid=2002)
    src = config.SRC_DIR / "beta"
    (src / "backend").mkdir(parents=True)
    (src / "backend" / "requirements.txt").write_text("fastapi\n")
    (src / "backend" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")

    alive(monkeypatch, False)
    started: list[str] = []

    def stop_or_refuse(app_row):
        if app_row["name"] == "alpha":
            raise psutil.AccessDenied(2780)

    monkeypatch.setattr(supervisor, "stop", stop_or_refuse)
    monkeypatch.setattr(native, "start", lambda *a, **k: (
        started.append(a[0]) or native.Processes(front_pid=9002, backend_pid=9001)))

    supervisor.restore_on_startup()  # must not raise

    assert started == ["beta"], "the other apps still come back"
    assert db.get_app_by_name("alpha")["status"] == "failed"


def test_a_failed_startup_says_so_in_the_apps_own_log(live_app, monkeypatch):
    """Whoever looks at the app needs to see why, not an empty log."""
    alive(monkeypatch, False)
    monkeypatch.setattr(
        supervisor, "stop",
        lambda app_row: (_ for _ in ()).throw(RuntimeError("no such luck")),
    )

    supervisor.restore_on_startup()

    log = (config.LOG_DIR / "alpha" / "runtime.log").read_text(encoding="utf-8")
    assert "no such luck" in log
