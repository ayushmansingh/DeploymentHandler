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
    monkeypatch.setattr(native, "stop", lambda procs: None)
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
    monkeypatch.setattr(native, "stop", lambda procs: stopped.append(procs))

    supervisor.stop(live_app)

    assert stopped[0].front_pid == 1002 and stopped[0].backend_pid == 1001
    after = db.get_app_by_name("alpha")
    assert after["pid"] is None and after["front_pid"] is None


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
