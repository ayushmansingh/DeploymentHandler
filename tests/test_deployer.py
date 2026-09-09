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


def test_locked_source_files_stop_the_app_rather_than_failing(data_dir, log, monkeypatch):
    """Windows will not delete a file the running app still has open.

    A SQLite database opened inside the app's own folder is the usual case,
    and it must not turn a replace into a failed deploy.
    """
    from launcher import archive, deployer, files, supervisor

    db.init()
    app_id = db.create_app("alpha")
    db.update_app(app_id, status="live", front_pid=1002)
    row = db.get_app(app_id)

    src = config.SRC_DIR / "alpha"
    src.mkdir(parents=True)
    (src / "app.db").write_text("locked")

    monkeypatch.setattr(config, "RUNTIME", "native")
    stopped: list = []
    monkeypatch.setattr(supervisor, "stop", stopped.append)
    # Removal fails while the app runs, and succeeds once it has been stopped.
    monkeypatch.setattr(files, "remove_tree", lambda path: bool(stopped))

    deployer._clear_source_dir(row, src, log)

    assert stopped, "the app must be stopped to release the file handles"
    assert "cannot be replaced while it is up" in log.read()


def test_source_that_cannot_be_cleared_at_all_is_reported(data_dir, log, monkeypatch):
    from launcher import archive, deployer, files, supervisor

    db.init()
    app_id = db.create_app("alpha")
    row = db.get_app(app_id)
    src = config.SRC_DIR / "alpha"
    src.mkdir(parents=True)

    monkeypatch.setattr(config, "RUNTIME", "native")
    monkeypatch.setattr(supervisor, "stop", lambda r: None)
    monkeypatch.setattr(files, "remove_tree", lambda path: False)

    with pytest.raises(archive.ArchiveError, match="could not be removed"):
        deployer._clear_source_dir(row, src, log)


def test_nothing_happens_when_there_is_no_previous_version(data_dir, log, monkeypatch):
    from launcher import deployer, supervisor

    db.init()
    row = db.get_app(db.create_app("alpha"))
    monkeypatch.setattr(config, "RUNTIME", "native")
    monkeypatch.setattr(
        supervisor, "stop",
        lambda r: pytest.fail("must not stop anything on a first deploy"),
    )

    deployer._clear_source_dir(row, config.SRC_DIR / "alpha", log)
    assert log.read() == ""
