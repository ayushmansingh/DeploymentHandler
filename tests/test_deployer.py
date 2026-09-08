"""Behaviour around failed deploys, under both runtimes.

The important property: a failed build must not report an app as down while
the previous version is still serving traffic.
"""
import pytest

from launcher import config, db, deployer, native, runtime


@pytest.fixture
def app_row(data_dir):
    db.init()
    app_id = db.create_app("alpha")
    db.update_app(
        app_id, container_id="abc123", status="building", host_port=24817, front_pid=999
    )
    return db.get_app(app_id)


@pytest.fixture
def log(tmp_path):
    return deployer._Log(tmp_path / "deploy.log")


@pytest.fixture(params=["native", "docker"])
def serving(request, monkeypatch):
    """Control whether the previous version looks alive, for each runtime."""
    monkeypatch.setattr(config, "RUNTIME", request.param)

    def set_state(alive: bool) -> None:
        if request.param == "native":
            monkeypatch.setattr(native, "is_running", lambda pid, marker=None: alive)
        else:
            monkeypatch.setattr(
                runtime, "container_state", lambda cid: "running" if alive else "missing"
            )

    return set_state


def test_app_stays_live_when_build_fails_before_the_swap(app_row, log, serving):
    serving(True)
    deployer._apply_failure_status(app_row, log, swapped=False)

    assert db.get_app(int(app_row["id"]))["status"] == "live"
    assert "nothing was taken offline" in log.read()


def test_app_is_failed_when_nothing_was_running(app_row, log, serving):
    serving(False)
    deployer._apply_failure_status(app_row, log, swapped=False)

    assert db.get_app(int(app_row["id"]))["status"] == "failed"


def test_app_is_failed_once_the_new_version_has_taken_over(app_row, log, serving):
    # After the swap the running processes are the broken new ones, so a
    # running state must not be mistaken for the old version still serving.
    serving(True)
    deployer._apply_failure_status(app_row, log, swapped=True)

    assert db.get_app(int(app_row["id"]))["status"] == "failed"


def test_first_ever_deploy_failing_leaves_the_app_failed(data_dir, log, serving):
    serving(False)
    db.init()
    app_id = db.create_app("brand-new")
    deployer._apply_failure_status(db.get_app(app_id), log, swapped=False)

    assert db.get_app(app_id)["status"] == "failed"
