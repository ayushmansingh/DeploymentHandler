"""Behaviour around failed deploys.

The important property: a failed build must not report an app as down while
the previous version is still serving traffic.
"""
from pathlib import Path

import pytest

from launcher import db, deployer, runtime


@pytest.fixture
def app_row(data_dir):
    db.init()
    app_id = db.create_app("alpha")
    db.update_app(app_id, container_id="abc123", status="building")
    return db.get_app(app_id)


@pytest.fixture
def log(tmp_path):
    return deployer._Log(tmp_path / "deploy.log")


def test_app_stays_live_when_build_fails_before_the_swap(app_row, log, monkeypatch):
    monkeypatch.setattr(runtime, "container_state", lambda cid: "running")
    deployer._apply_failure_status(app_row, log, swapped=False)

    assert db.get_app(int(app_row["id"]))["status"] == "live"
    assert "nothing was taken offline" in log.read()


def test_app_is_failed_when_nothing_was_running(app_row, log, monkeypatch):
    monkeypatch.setattr(runtime, "container_state", lambda cid: "missing")
    deployer._apply_failure_status(app_row, log, swapped=False)

    assert db.get_app(int(app_row["id"]))["status"] == "failed"


def test_app_is_failed_once_the_new_container_has_taken_over(app_row, log, monkeypatch):
    # After the swap the running container is the broken new one, so a running
    # state must not be mistaken for the old version still serving.
    monkeypatch.setattr(runtime, "container_state", lambda cid: "running")
    deployer._apply_failure_status(app_row, log, swapped=True)

    assert db.get_app(int(app_row["id"]))["status"] == "failed"


def test_first_ever_deploy_failing_leaves_the_app_failed(data_dir, log, monkeypatch):
    db.init()
    app_id = db.create_app("brand-new")
    monkeypatch.setattr(runtime, "container_state", lambda cid: "missing")
    deployer._apply_failure_status(db.get_app(app_id), log, swapped=False)

    assert db.get_app(app_id)["status"] == "failed"
