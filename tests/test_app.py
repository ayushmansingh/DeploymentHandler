"""Endpoint behaviour for uploading and replacing apps."""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from launcher import app as web
from launcher import db, deployer, runtime


@pytest.fixture
def client(data_dir, monkeypatch):
    """A test client whose deploys are recorded rather than executed."""
    queued: list[int] = []
    monkeypatch.setattr(deployer, "enqueue", queued.append)
    monkeypatch.setattr(runtime, "docker_available", lambda: True)
    monkeypatch.setattr(runtime, "container_state", lambda cid: "running")
    monkeypatch.setattr(runtime, "stop_container", lambda cid, remove=True: None)
    with TestClient(web.app) as c:
        c.queued = queued  # type: ignore[attr-defined]
        yield c


def zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


SAMPLE = {"backend/requirements.txt": "fastapi\n", "backend/main.py": "app = 1\n"}


def upload(client, name="sales-dashboard", **form):
    return client.post(
        "/upload",
        data={"name": name, "uploaded_by": form.pop("uploaded_by", "Priya"), **form},
        files={"file": ("app.zip", zip_bytes(SAMPLE), "application/zip")},
        follow_redirects=False,
    )


def test_upload_creates_app_and_queues_a_deploy(client):
    response = upload(client)
    assert response.status_code == 303
    assert db.get_app_by_name("sales-dashboard") is not None
    assert len(client.queued) == 1


def test_upload_normalises_the_name(client):
    upload(client, name="Sales Dashboard")
    assert db.get_app_by_name("sales-dashboard") is not None


def test_upload_rejects_an_unusable_name(client):
    response = upload(client, name="ab")
    assert response.status_code == 400
    assert "at least 3 letters" in response.text


def test_replace_reuses_the_existing_app_and_keeps_its_port(client):
    upload(client)
    row = db.get_app_by_name("sales-dashboard")
    db.update_app(int(row["id"]), host_port=24817, status="live", container_id="abc")

    response = client.post(
        "/app/sales-dashboard/replace",
        data={"uploaded_by": "Ayushman"},
        files={"file": ("new.zip", zip_bytes(SAMPLE), "application/zip")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    after = db.get_app_by_name("sales-dashboard")
    assert after["host_port"] == 24817, "replacing must not change the app's link"
    assert len(db.list_deploys(int(after["id"]))) == 2
    assert len(client.queued) == 2


def test_replace_leaves_the_current_version_running_by_default(client):
    upload(client)
    row = db.get_app_by_name("sales-dashboard")
    db.update_app(int(row["id"]), status="live", container_id="abc")

    client.post(
        "/app/sales-dashboard/replace",
        files={"file": ("new.zip", zip_bytes(SAMPLE), "application/zip")},
        follow_redirects=False,
    )
    assert db.get_app_by_name("sales-dashboard")["status"] == "live"


def test_replace_can_stop_the_current_version_first(client):
    upload(client)
    row = db.get_app_by_name("sales-dashboard")
    db.update_app(int(row["id"]), status="live", container_id="abc")

    stopped: list[str] = []
    from launcher import app as web_module
    web_module.runtime.stop_container = lambda cid, remove=True: stopped.append(cid)

    client.post(
        "/app/sales-dashboard/replace",
        data={"stop_current": "1"},
        files={"file": ("new.zip", zip_bytes(SAMPLE), "application/zip")},
        follow_redirects=False,
    )
    assert stopped == ["abc"]
    assert db.get_app_by_name("sales-dashboard")["status"] == "stopped"


def test_replace_on_an_unknown_app_is_a_404(client):
    response = client.post(
        "/app/nope/replace",
        files={"file": ("new.zip", zip_bytes(SAMPLE), "application/zip")},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_replace_without_a_file_explains_itself(client):
    upload(client)
    response = client.post(
        "/app/sales-dashboard/replace", data={}, follow_redirects=False
    )
    assert response.status_code == 400
    assert "choose a ZIP" in response.text


def test_oversized_upload_is_rejected_with_a_plain_message(client, monkeypatch):
    from launcher import config
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 50)
    response = client.post(
        "/upload",
        data={"name": "big-app", "uploaded_by": "Priya"},
        files={"file": ("app.zip", zip_bytes({"a.py": "x" * 5000}), "application/zip")},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "node_modules" in response.text


def test_dashboard_lists_apps_with_their_links(client):
    upload(client, name="sales-dashboard")
    row = db.get_app_by_name("sales-dashboard")
    db.update_app(int(row["id"]), status="live", host_port=24817)

    page = client.get("/").text
    assert "sales-dashboard" in page
    assert "24817" in page


def test_live_app_card_links_to_the_app_itself(client):
    """Clicking an app on the dashboard must open the app, not its settings."""
    upload(client, name="sales-dashboard")
    row = db.get_app_by_name("sales-dashboard")
    db.update_app(int(row["id"]), status="live", host_port=24817)

    payload = client.get("/api/apps").json()
    card = payload["apps"][0]
    assert card["url"].endswith(":24817")
    assert payload["live_count"] == 1

    html = client.get("/partials/apps").text
    assert 'href="http://' in html and "24817" in html
    assert 'href="/app/sales-dashboard"' in html, "Manage link should still be there"


def test_app_that_is_not_running_links_to_its_page_instead(client):
    upload(client, name="broken-report")
    row = db.get_app_by_name("broken-report")
    db.update_app(int(row["id"]), status="failed")

    html = client.get("/partials/apps").text
    assert 'href="/app/broken-report"' in html
    assert "no address yet" in html
    assert client.get("/api/apps").json()["live_count"] == 0


def test_running_apps_are_listed_before_broken_ones(client):
    upload(client, name="zzz-working")
    upload(client, name="aaa-broken")
    db.update_app(int(db.get_app_by_name("zzz-working")["id"]),
                  status="live", host_port=24817)
    db.update_app(int(db.get_app_by_name("aaa-broken")["id"]), status="failed")

    names = [a["name"] for a in client.get("/api/apps").json()["apps"]]
    assert names == ["zzz-working", "aaa-broken"]


def test_empty_dashboard_explains_what_to_do(client):
    page = client.get("/").text
    assert "No applications yet" in page


def test_delete_removes_the_app_and_its_files(client, data_dir):
    from launcher import appdata, config
    upload(client, name="sales-dashboard")
    row = db.get_app_by_name("sales-dashboard")
    db.update_app(int(row["id"]), status="live", host_port=24817)

    (config.SRC_DIR / "sales-dashboard").mkdir(parents=True, exist_ok=True)
    (config.SRC_DIR / "sales-dashboard" / "main.py").write_text("x = 1")
    (appdata.dir_for("sales-dashboard") / "report.csv").write_text("data\n")

    response = client.post("/app/sales-dashboard/delete", follow_redirects=False)

    assert response.status_code == 303
    assert db.get_app_by_name("sales-dashboard") is None
    assert not (config.SRC_DIR / "sales-dashboard").exists()
    assert not (config.APPDATA_DIR / "sales-dashboard").exists()
    assert not (config.UPLOAD_DIR / "sales-dashboard").exists()


def test_delete_frees_the_port_for_reuse(client):
    from launcher import ports
    upload(client, name="sales-dashboard")
    row = db.get_app_by_name("sales-dashboard")
    port = ports.allocate(int(row["id"]))

    client.post("/app/sales-dashboard/delete", follow_redirects=False)

    assert port not in ports.in_use()


def test_delete_reports_files_it_could_not_remove(client, monkeypatch):
    """A locked file must not be hidden - the app is gone either way."""
    from launcher import files
    upload(client, name="sales-dashboard")
    monkeypatch.setattr(files, "remove_tree", lambda path: False)

    response = client.post("/app/sales-dashboard/delete", follow_redirects=False)

    assert response.status_code == 200
    assert "still on the server" in response.text
    assert db.get_app_by_name("sales-dashboard") is None, "the app is still deleted"


def test_status_partial_reports_whether_the_deploy_settled(client):
    """The page polls on this marker, so it decides when polling stops."""
    upload(client, name="sales-dashboard")
    row = db.get_app_by_name("sales-dashboard")
    deploy = db.list_deploys(int(row["id"]))[0]

    html = client.get("/app/sales-dashboard/partial-status").text
    assert 'data-deploy-status="queued"' in html

    db.finish_deploy(int(deploy["id"]), "live")
    db.update_app(int(row["id"]), status="live", host_port=24817)
    html = client.get("/app/sales-dashboard/partial-status").text
    assert 'data-deploy-status="live"' in html
    assert "24817" in html


def test_grid_declares_when_nothing_is_building(client):
    """Drives the dashboard's back-off; wrong here means constant polling."""
    upload(client, name="sales-dashboard")
    assert 'data-busy="1"' in client.get("/partials/apps").text

    row = db.get_app_by_name("sales-dashboard")
    db.finish_deploy(int(db.list_deploys(int(row["id"]))[0]["id"]), "live")
    db.update_app(int(row["id"]), status="live", host_port=24817)
    assert 'data-busy="0"' in client.get("/partials/apps").text


def test_app_page_does_not_reload_itself(client):
    """A scheduled reload cancels form posts - it broke the delete button."""
    upload(client, name="sales-dashboard")
    page = client.get("/app/sales-dashboard").text
    assert "location.reload" not in page


def test_saving_a_setting_from_the_page(client):
    from launcher import db as database
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")

    response = client.post(
        "/app/sales-dashboard/settings",
        data={"key": "REDASH_API_KEY", "value": "secret123", "updated_by": "Priya"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert database.settings_env(int(row["id"])) == {"REDASH_API_KEY": "secret123"}


def test_the_page_shows_the_name_masked_and_never_the_value(client):
    """A value must not be recoverable by opening the page."""
    from launcher import db as database, settings as s
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.set_setting(int(row["id"]), "REDASH_API_KEY", "secret123", "Priya")

    page = client.get("/app/sales-dashboard").text

    assert "REDASH_API_KEY" in page
    assert s.MASK in page
    assert "secret123" not in page, "the value leaked into the page"


def test_a_bad_setting_name_is_explained(client):
    upload(client, name="sales-dashboard")
    response = client.post(
        "/app/sales-dashboard/settings",
        data={"key": "2FA TOKEN", "value": "x"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "setting_error" in response.headers["location"]
    page = client.get(response.headers["location"]).text
    assert "not a usable name" in page


def test_reserved_names_are_refused_through_the_page(client):
    from launcher import db as database
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")

    response = client.post(
        "/app/sales-dashboard/settings",
        data={"key": "APP_DATA_DIR", "value": "/somewhere/else"},
        follow_redirects=False,
    )

    assert "setting_error" in response.headers["location"]
    assert database.settings_env(int(row["id"])) == {}


def test_removing_a_setting_from_the_page(client):
    from launcher import db as database
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.set_setting(int(row["id"]), "TOKEN", "secret")

    response = client.post(
        "/app/sales-dashboard/settings/TOKEN/delete", follow_redirects=False
    )

    assert response.status_code == 303
    assert database.settings_env(int(row["id"])) == {}


def test_changing_a_setting_restarts_a_running_app(client, monkeypatch):
    """Otherwise the app carries on with the old value and looks broken."""
    from launcher import db as database, supervisor
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.update_app(int(row["id"]), status="live", host_port=24817)

    restarted = []
    monkeypatch.setattr(supervisor, "launch", lambda r, reason="": restarted.append(r["name"]))

    client.post(
        "/app/sales-dashboard/settings",
        data={"key": "TOKEN", "value": "secret"},
        follow_redirects=False,
    )
    assert restarted == ["sales-dashboard"]


def test_changing_a_setting_does_not_start_a_stopped_app(client, monkeypatch):
    from launcher import db as database, supervisor
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.update_app(int(row["id"]), status="stopped")

    monkeypatch.setattr(
        supervisor, "launch",
        lambda r, reason="": pytest.fail("a stopped app must stay stopped"),
    )
    client.post(
        "/app/sales-dashboard/settings",
        data={"key": "TOKEN", "value": "secret"},
        follow_redirects=False,
    )


def _needs(client, name, *names):
    from launcher import db as database
    upload(client, name=name)
    row = database.get_app_by_name(name)
    database.set_declared_settings(
        int(row["id"]), [{"name": n, "description": ""} for n in names]
    )
    return database.get_app_by_name(name)


def test_an_app_waiting_on_settings_says_what_it_needs(client):
    from launcher import db as database
    row = _needs(client, "sales-dashboard", "REDASH_API_KEY")
    database.update_app(int(row["id"]), status="needs_setup")

    # Collapse whitespace: the template wraps these lines, and where it wraps
    # is not something a test should care about.
    page = " ".join(client.get("/app/sales-dashboard").text.split())
    assert "waiting for 1 setting" in page
    assert "REDASH_API_KEY" in page

    card = client.get("/api/apps").json()["apps"][0]
    assert card["missing_settings"] == ["REDASH_API_KEY"]


def test_supplying_the_last_setting_starts_it(client, monkeypatch):
    from launcher import db as database, supervisor
    row = _needs(client, "sales-dashboard", "REDASH_API_KEY")
    database.update_app(int(row["id"]), status="needs_setup")

    started = []
    monkeypatch.setattr(supervisor, "launch", lambda r, reason="": started.append(r["name"]))

    client.post(
        "/app/sales-dashboard/settings",
        data={"key": "REDASH_API_KEY", "value": "secret"},
        follow_redirects=False,
    )
    assert started == ["sales-dashboard"]


def test_it_keeps_waiting_while_anything_is_outstanding(client, monkeypatch):
    from launcher import db as database, supervisor
    row = _needs(client, "sales-dashboard", "REDASH_API_KEY", "REPORT_EMAIL")
    database.update_app(int(row["id"]), status="needs_setup")

    monkeypatch.setattr(
        supervisor, "launch",
        lambda r, reason="": pytest.fail("must not start with a setting outstanding"),
    )
    client.post(
        "/app/sales-dashboard/settings",
        data={"key": "REDASH_API_KEY", "value": "secret"},
        follow_redirects=False,
    )
    assert database.get_app_by_name("sales-dashboard")["status"] == "needs_setup"


def test_removing_a_declared_setting_stops_a_running_app(client, monkeypatch):
    """Carrying on without it is the half-configured state being avoided."""
    from launcher import db as database, supervisor
    row = _needs(client, "sales-dashboard", "REDASH_API_KEY")
    database.set_setting(int(row["id"]), "REDASH_API_KEY", "secret")
    database.update_app(int(row["id"]), status="live", host_port=24817, front_pid=99)

    stopped = []
    monkeypatch.setattr(supervisor, "stop", stopped.append)
    monkeypatch.setattr(
        supervisor, "launch",
        lambda r, reason="": pytest.fail("it should stop, not restart"),
    )

    client.post(
        "/app/sales-dashboard/settings/REDASH_API_KEY/delete", follow_redirects=False
    )

    assert stopped, "the app must be stopped"
    assert database.get_app_by_name("sales-dashboard")["status"] == "needs_setup"


def test_removing_an_undeclared_setting_only_restarts(client, monkeypatch):
    from launcher import db as database, supervisor
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.set_setting(int(row["id"]), "EXTRA", "value")
    database.update_app(int(row["id"]), status="live", host_port=24817)

    restarted = []
    monkeypatch.setattr(supervisor, "launch", lambda r, reason="": restarted.append(r["name"]))

    client.post("/app/sales-dashboard/settings/EXTRA/delete", follow_redirects=False)

    assert restarted == ["sales-dashboard"]
    assert database.get_app_by_name("sales-dashboard")["status"] == "live"


def test_apps_waiting_for_setup_sort_above_broken_ones(client):
    from launcher import db as database
    upload(client, name="aaa-broken")
    upload(client, name="zzz-waiting")
    database.update_app(int(database.get_app_by_name("aaa-broken")["id"]), status="failed")
    database.update_app(int(database.get_app_by_name("zzz-waiting")["id"]), status="needs_setup")

    names = [a["name"] for a in client.get("/api/apps").json()["apps"]]
    assert names == ["zzz-waiting", "aaa-broken"]


def test_the_waiting_message_clears_once_the_app_starts(client, monkeypatch):
    """Otherwise the page keeps asking for something it already has."""
    from launcher import db as database, supervisor
    row = _needs(client, "sales-dashboard", "REDASH_API_KEY")
    deploy_id = int(database.list_deploys(int(row["id"]))[0]["id"])
    database.finish_deploy(deploy_id, "needs_setup", "needs REDASH_API_KEY")
    database.update_app(int(row["id"]), status="needs_setup")

    monkeypatch.setattr(supervisor, "launch", lambda r, reason="": None)
    client.post(
        "/app/sales-dashboard/settings",
        data={"key": "REDASH_API_KEY", "value": "secret"},
        follow_redirects=False,
    )

    after = database.get_deploy(deploy_id)
    assert after["status"] == "live"
    assert after["error_summary"] is None
    page = " ".join(client.get("/app/sales-dashboard").text.split())
    assert "Waiting for settings" not in page


def test_a_new_version_waiting_on_a_setting_leaves_the_old_one_serving(client, monkeypatch):
    """A working app must not go down because its replacement needs configuring."""
    from launcher import db as database, supervisor
    row = _needs(client, "sales-dashboard", "SLACK_WEBHOOK")
    database.update_app(int(row["id"]), status="live", host_port=24817, front_pid=99)
    deploy_id = int(database.list_deploys(int(row["id"]))[0]["id"])
    database.finish_deploy(deploy_id, "needs_setup", "needs SLACK_WEBHOOK")

    launched = []
    monkeypatch.setattr(supervisor, "launch", lambda r, reason="": launched.append(reason))

    # Still live while it waits.
    assert database.get_app_by_name("sales-dashboard")["status"] == "live"
    assert launched == []

    client.post(
        "/app/sales-dashboard/settings",
        data={"key": "SLACK_WEBHOOK", "value": "https://hooks.example"},
        follow_redirects=False,
    )

    assert launched and "new version" in launched[0]
    assert database.get_deploy(deploy_id)["status"] == "live"


def test_each_tab_marks_itself_current(client):
    """Otherwise every tab looks inactive and nobody knows where they are."""
    assert 'class="tab on" href="/"' in client.get("/").text
    assert 'class="tab on" href="/deploy"' in client.get("/deploy").text
    assert 'class="tab on" href="/admin/update"' in client.get("/admin/update").text


def test_the_deploy_tab_carries_the_upload_form(client):
    page = client.get("/deploy").text
    assert 'action="/upload"' in page
    assert "copy-prompt" in page
    assert "dropzone" in page


def test_the_dashboard_no_longer_carries_the_upload_form(client):
    """Splitting them is the point; a stray second form would be confusing."""
    assert 'action="/upload"' not in client.get("/").text


def test_the_dashboard_tab_counts_the_apps(client):
    assert '<span class="count">' not in client.get("/").text

    upload(client, name="sales-dashboard")
    assert '<span class="count">1</span>' in client.get("/").text


def test_deploy_is_not_usable_as_an_app_name(client):
    """It is a route now, so an app of that name would shadow the tab."""
    response = upload(client, name="deploy")
    assert response.status_code == 400
    assert "reserved" in response.text
