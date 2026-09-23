"""Endpoint behaviour for uploading and replacing apps."""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from launcher import app as web
from launcher import db, deployer, runtime


def launches_into(record, field="name", ok=True):
    """Stand-in for supervisor.launch, which returns True when it started.

    Returning None here - as these mocks used to - made every caller look as
    though it had failed to start, and hid the fact that one caller was
    ignoring the answer altogether.
    """
    def launch(row, reason=""):
        record.append(reason if field == "reason" else row[field])
        return ok
    return launch


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
    monkeypatch.setattr(supervisor, "launch", launches_into(restarted))

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
    monkeypatch.setattr(supervisor, "launch", launches_into(started))

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
    monkeypatch.setattr(supervisor, "launch", launches_into(restarted))

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

    monkeypatch.setattr(supervisor, "launch", launches_into([]))
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
    monkeypatch.setattr(supervisor, "launch", launches_into(launched, "reason"))

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


def test_several_settings_can_be_filled_in_one_go(client, monkeypatch):
    """Otherwise each one costs a page reload before the app can start."""
    from launcher import db as database, supervisor
    row = _needs(client, "sales-dashboard", "REDASH_API_KEY", "REPORT_EMAIL", "SLACK_WEBHOOK")
    database.update_app(int(row["id"]), status="needs_setup")

    started = []
    monkeypatch.setattr(supervisor, "launch", launches_into(started))

    response = client.post(
        "/app/sales-dashboard/settings/fill",
        data={
            "setting__REDASH_API_KEY": "key-123",
            "setting__REPORT_EMAIL": "team@example.com",
            "setting__SLACK_WEBHOOK": "https://hooks.example",
            "updated_by": "Priya",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert database.settings_env(int(row["id"])) == {
        "REDASH_API_KEY": "key-123",
        "REPORT_EMAIL": "team@example.com",
        "SLACK_WEBHOOK": "https://hooks.example",
    }
    assert started == ["sales-dashboard"]


def test_filling_only_some_leaves_the_app_waiting(client, monkeypatch):
    from launcher import db as database, supervisor
    row = _needs(client, "sales-dashboard", "REDASH_API_KEY", "REPORT_EMAIL")
    database.update_app(int(row["id"]), status="needs_setup")

    monkeypatch.setattr(
        supervisor, "launch",
        lambda r, reason="": pytest.fail("must not start with one still outstanding"),
    )
    client.post(
        "/app/sales-dashboard/settings/fill",
        data={"setting__REDASH_API_KEY": "key-123", "setting__REPORT_EMAIL": "  "},
        follow_redirects=False,
    )

    assert [d["name"] for d in database.missing_settings(int(row["id"]))] == ["REPORT_EMAIL"]
    assert database.get_app_by_name("sales-dashboard")["status"] == "needs_setup"


def test_filling_nothing_says_so(client):
    row = _needs(client, "sales-dashboard", "REDASH_API_KEY")
    response = client.post(
        "/app/sales-dashboard/settings/fill",
        data={"setting__REDASH_API_KEY": "   ", "updated_by": "Priya"},
        follow_redirects=False,
    )
    assert "setting_error" in response.headers["location"]


def test_the_waiting_form_offers_a_field_per_setting(client):
    from launcher import db as database
    row = _needs(client, "sales-dashboard", "REDASH_API_KEY", "REPORT_EMAIL")
    database.update_app(int(row["id"]), status="needs_setup")

    page = client.get("/app/sales-dashboard").text

    assert 'name="setting__REDASH_API_KEY"' in page
    assert 'name="setting__REPORT_EMAIL"' in page
    assert "no need to upload the ZIP again" in " ".join(page.split())


def test_values_are_never_echoed_into_the_waiting_form(client):
    from launcher import db as database
    row = _needs(client, "sales-dashboard", "REDASH_API_KEY", "REPORT_EMAIL")
    database.set_setting(int(row["id"]), "REDASH_API_KEY", "already-secret")
    database.update_app(int(row["id"]), status="needs_setup")

    page = client.get("/app/sales-dashboard").text

    assert "already-secret" not in page
    assert 'name="setting__REDASH_API_KEY"' not in page, "already set, so not outstanding"
    assert 'name="setting__REPORT_EMAIL"' in page


def test_a_waiting_app_meets_the_settings_form_first(client):
    """Leading with "Replace with a newer ZIP" reads as "upload it again",
    which is the one thing that is not needed."""
    from launcher import db as database
    row = _needs(client, "sales-dashboard", "REDASH_API_KEY")
    database.update_app(int(row["id"]), status="needs_setup")

    page = client.get("/app/sales-dashboard").text
    assert page.index("waiting for 1") < page.index("Replace with a newer ZIP")


def test_a_working_app_meets_replace_first(client):
    from launcher import db as database
    upload(client, name="sales-dashboard")
    database.update_app(
        int(database.get_app_by_name("sales-dashboard")["id"]),
        status="live", host_port=24817,
    )

    page = client.get("/app/sales-dashboard").text
    assert page.index("Replace with a newer ZIP") < page.index("<strong>Settings</strong>")


def test_an_app_starts_with_an_optional_setting_unset(client, monkeypatch):
    from launcher import db as database, supervisor
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.set_declared_settings(int(row["id"]), [
        {"name": "REDASH_API_KEY", "description": "", "required": True},
        {"name": "SLACK_WEBHOOK", "description": "", "required": False},
    ])
    database.update_app(int(row["id"]), status="needs_setup")

    started = []
    monkeypatch.setattr(supervisor, "launch", launches_into(started))

    client.post(
        "/app/sales-dashboard/settings/fill",
        data={"setting__REDASH_API_KEY": "secret"},
        follow_redirects=False,
    )
    assert started == ["sales-dashboard"], "the optional one must not hold it back"


def test_an_optional_setting_is_offered_and_labelled(client):
    from launcher import db as database
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.set_declared_settings(int(row["id"]), [
        {"name": "SLACK_WEBHOOK", "description": "Where alerts go", "required": False},
    ])
    database.update_app(int(row["id"]), status="live", host_port=24817)

    page = " ".join(client.get("/app/sales-dashboard").text.split())
    assert 'name="setting__SLACK_WEBHOOK"' in page
    assert "optional" in page
    assert "waiting for" not in page, "an optional setting is not something to wait for"


def test_removing_an_optional_setting_only_restarts(client, monkeypatch):
    from launcher import db as database, supervisor
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.set_declared_settings(int(row["id"]), [
        {"name": "SLACK_WEBHOOK", "description": "", "required": False},
    ])
    database.set_setting(int(row["id"]), "SLACK_WEBHOOK", "https://hooks.example")
    database.update_app(int(row["id"]), status="live", host_port=24817)

    restarted = []
    monkeypatch.setattr(supervisor, "launch", launches_into(restarted))

    client.post("/app/sales-dashboard/settings/SLACK_WEBHOOK/delete", follow_redirects=False)

    assert restarted == ["sales-dashboard"]
    assert database.get_app_by_name("sales-dashboard")["status"] == "live"


def test_a_defaulted_setting_shows_its_value_instead_of_asking(client):
    from launcher import db as database
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.set_declared_settings(int(row["id"]), [
        {"name": "PAGE_SIZE", "description": "Rows per page",
         "required": True, "default": "50"},
    ])
    database.update_app(int(row["id"]), status="live", host_port=24817)

    page = " ".join(client.get("/app/sales-dashboard").text.split())
    assert "from the app" in page
    assert ">50<" in page, "a value that shipped in the ZIP is not a secret"
    assert 'name="setting__PAGE_SIZE"' not in page, "nobody should be asked for it"
    assert "waiting for" not in page


def test_a_secret_stays_masked_while_a_default_does_not(client):
    from launcher import db as database
    from launcher import settings as app_settings
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.set_declared_settings(int(row["id"]), [
        {"name": "PAGE_SIZE", "description": "", "required": True, "default": "50"},
        {"name": "REDASH_API_KEY", "description": "", "required": True, "default": None},
    ])
    database.set_setting(int(row["id"]), "REDASH_API_KEY", "super-secret-token")
    database.update_app(int(row["id"]), status="live", host_port=24817)

    page = client.get("/app/sales-dashboard").text
    assert "super-secret-token" not in page
    assert app_settings.MASK in page
    assert ">50<" in " ".join(page.split())


def test_overriding_a_default_is_shown_as_an_override(client, monkeypatch):
    from launcher import db as database, supervisor
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.set_declared_settings(int(row["id"]), [
        {"name": "PAGE_SIZE", "description": "", "required": True, "default": "50"},
    ])
    database.update_app(int(row["id"]), status="live", host_port=24817)
    monkeypatch.setattr(supervisor, "launch", launches_into([]))

    client.post(
        "/app/sales-dashboard/settings",
        data={"key": "PAGE_SIZE", "value": "200", "updated_by": "Priya"},
        follow_redirects=False,
    )

    page = " ".join(client.get("/app/sales-dashboard").text.split())
    assert ">200<" in page
    assert "overrides the app's" in page
    assert "Reset" in page, "removing an override restores the default, not nothing"


def test_resetting_an_override_restores_the_default_without_stopping_the_app(
    client, monkeypatch
):
    from launcher import db as database, supervisor
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    app_id = int(row["id"])
    database.set_declared_settings(app_id, [
        {"name": "PAGE_SIZE", "description": "", "required": True, "default": "50"},
    ])
    database.set_setting(app_id, "PAGE_SIZE", "200")
    database.update_app(app_id, status="live", host_port=24817)

    restarted = []
    monkeypatch.setattr(supervisor, "launch", launches_into(restarted))

    client.post("/app/sales-dashboard/settings/PAGE_SIZE/delete", follow_redirects=False)

    assert restarted == ["sales-dashboard"]
    assert database.get_app_by_name("sales-dashboard")["status"] == "live"
    assert database.effective_env(app_id)["PAGE_SIZE"] == "50"


def test_the_log_does_not_end_on_a_sentence_that_stopped_being_true(client, monkeypatch):
    """A held deploy's log ended on "Waiting for settings" and stayed that way
    once the app started, so anyone opening the log to check on a running app
    read that it was still waiting and concluded it had never come up."""
    from pathlib import Path

    from launcher import db as database, supervisor
    upload(client, name="query-allocation-dashboard")
    row = database.get_app_by_name("query-allocation-dashboard")
    app_id = int(row["id"])
    database.set_declared_settings(app_id, [
        {"name": "REDASH_API_ROOT", "description": "", "required": True},
    ])
    database.update_app(app_id, status="needs_setup", host_port=24817)

    deploy = database.list_deploys(app_id, limit=1)[0]
    log_path = Path(deploy["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "[launcher] Built successfully. Waiting for settings: REDASH_API_ROOT\n"
        "[launcher] Set them on this app's page and it will start.\n",
        encoding="utf-8",
    )
    database.finish_deploy(int(deploy["id"]), "needs_setup", "needs settings")

    monkeypatch.setattr(supervisor, "launch", launches_into([]))
    client.post(
        "/app/query-allocation-dashboard/settings/fill",
        data={"setting__REDASH_API_ROOT": "https://redash.example", "updated_by": "Priya"},
        follow_redirects=False,
    )

    log = client.get("/app/query-allocation-dashboard/log").text
    assert "Waiting for settings" in log, "the history stays; it did happen"
    assert "Settings supplied by Priya" in log
    assert "SUCCESS" in log
    # The host is whatever this machine calls itself, so assert the shape and
    # the port rather than a hostname that differs per server.
    last = log.rstrip().splitlines()[-1]
    assert last.startswith("[launcher] SUCCESS."), (
        "the last line has to describe the state the app is actually in"
    )
    assert last.endswith(":24817"), "and it has to name the port the app is on"


def test_the_app_own_log_is_readable_from_the_page(client):
    """A backend that dies after starting leaves its reason only in
    runtime.log, which was readable nowhere but the server's own disk."""
    from launcher import config
    upload(client, name="sales-dashboard")

    log_dir = config.LOG_DIR / "sales-dashboard"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "runtime.log").write_text(
        "Traceback (most recent call last):\n"
        "  File \"main.py\", line 3, in <module>\n"
        "ModuleNotFoundError: No module named 'pandas'\n",
        encoding="utf-8",
    )

    body = client.get("/app/sales-dashboard/runtime-log").text
    assert "ModuleNotFoundError" in body

    page = client.get("/app/sales-dashboard").text
    assert "/runtime-log" in page, "the page has to offer it, not just the route"


def test_the_app_own_log_says_so_when_there_is_nothing_yet(client):
    upload(client, name="sales-dashboard")
    body = client.get("/app/sales-dashboard/runtime-log").text
    assert "Nothing yet" in body


def test_a_huge_app_log_is_not_read_into_memory_to_show_its_tail(client, tmp_path):
    """uvicorn logs a line per request, so a dashboard that polls produces tens
    of megabytes a month. Reading all of it to render the last few hundred
    lines is how a log viewer takes a server down instead of helping debug
    one."""
    from launcher import config, files
    upload(client, name="sales-dashboard")

    log_dir = config.LOG_DIR / "sales-dashboard"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "runtime.log"
    with open(log, "w", encoding="utf-8") as fh:
        for n in range(200_000):
            fh.write(f'INFO: 127.0.0.1 - "GET /api/status HTTP/1.1" 200 OK  line={n}\n')
    size = log.stat().st_size
    assert size > 10_000_000, "the point of the test is that the file is large"

    body = client.get("/app/sales-dashboard/runtime-log").text

    assert "line=199999" in body, "the newest lines are the ones that matter"
    assert "line=0" not in body, "the oldest lines must not be dragged along"
    assert len(body) < size / 100, "only a small tail should ever be returned"

    # And the helper itself reads a window, not the file.
    assert files.tail_text(log, 3).splitlines() == [
        f'INFO: 127.0.0.1 - "GET /api/status HTTP/1.1" 200 OK  line={n}'
        for n in (199_997, 199_998, 199_999)
    ]


def test_the_tail_never_starts_on_half_a_line(client, tmp_path):
    """The read window lands at an arbitrary byte offset, so the first line it
    sees is usually a fragment. A truncated first line in a traceback is worse
    than no line at all."""
    from launcher import files
    log = tmp_path / "runtime.log"
    log.write_text("".join(f"{'x' * 300} line {n}\n" for n in range(500)), encoding="utf-8")

    for wanted in (1, 5, 50):
        got = files.tail_text(log, wanted).splitlines()
        assert len(got) == wanted
        assert all(line.startswith("x" * 300) for line in got), "no fragments"


def test_a_log_that_has_outgrown_its_cap_is_rolled_over_at_the_next_start(tmp_path):
    from launcher import files
    log = tmp_path / "runtime.log"
    log.write_text("old content\n" * 1000, encoding="utf-8")

    assert files.rotate_if_large(log, limit=10_000_000) is False, "small log stays put"
    assert files.rotate_if_large(log, limit=100) is True
    assert not log.exists(), "the live name is free for the next run"
    assert (tmp_path / "runtime.log.1").read_text(encoding="utf-8").startswith("old content")

    # Only one previous log is kept, so this cannot grow without bound either.
    log.write_text("newer content\n" * 1000, encoding="utf-8")
    assert files.rotate_if_large(log, limit=100) is True
    assert (tmp_path / "runtime.log.1").read_text(encoding="utf-8").startswith("newer content")


# ---------------------------------------------------------------------------
# Links have to survive the server getting a different address
# ---------------------------------------------------------------------------

def test_app_links_follow_the_address_the_dashboard_was_opened_on(client):
    """Links were built from a hostname typed once into settings.cmd. The
    office DHCP does not promise the same address twice, so after a reboot
    every link on the dashboard named an address the machine no longer had -
    while the server itself was perfectly fine."""
    from launcher import db as database
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.update_app(int(row["id"]), status="live", host_port=24817)

    # Two colleagues reach the same server by different names.
    first = client.get("/app/sales-dashboard/status",
                       headers={"host": "172.16.4.25:9000"}).json()
    second = client.get("/app/sales-dashboard/status",
                        headers={"host": "mmt11842:9000"}).json()

    assert first["url"] == "http://172.16.4.25:24817"
    assert second["url"] == "http://mmt11842:24817"


def test_a_stale_configured_host_cannot_poison_a_link(client, monkeypatch):
    """Even with the old address still sitting in settings.cmd, a browser that
    reached us on the new one must be sent back to the new one."""
    from launcher import config, db as database
    monkeypatch.setattr(config, "PUBLIC_HOST", "172.16.4.30")   # yesterday's address
    upload(client, name="sales-dashboard")
    row = database.get_app_by_name("sales-dashboard")
    database.update_app(int(row["id"]), status="live", host_port=24817)

    payload = client.get("/app/sales-dashboard/status",
                         headers={"host": "172.16.4.25:9000"}).json()

    assert payload["url"] == "http://172.16.4.25:24817"
    assert "4.30" not in payload["url"], "the stale setting must not win"


def test_the_server_tab_offers_the_name_rather_than_the_address(client):
    from launcher import hostinfo
    page = client.get("/admin/update", headers={"host": "172.16.4.25:9000"}).text

    assert "The link to share" in page
    assert hostinfo.computer_name() in page, "the name is the stable part"
    assert "172.16.4.25" in page, "and the address we were reached on is shown too"


# ---------------------------------------------------------------------------
# A picture of each app, for the dashboard
# ---------------------------------------------------------------------------

def test_a_card_falls_back_to_a_tile_when_there_is_no_picture(client):
    """A server with no browser, or an app that has never been live, must
    still give the card something to show - a grey hole reads as broken."""
    upload(client, name="sales-dashboard")

    page = client.get("/").text
    assert 'class="app-shot-tile"' in page, "the fallback is drawn, not left empty"
    assert "/thumb" not in page, "and no image is requested that would 404"


def test_the_fallback_tile_is_stable_and_per_app():
    """Two apps should not look alike, and one app should not change colour
    on every refresh - that is what makes it read as a design."""
    from launcher import thumbs

    first = thumbs.tile("sales-tracker")
    assert first == thumbs.tile("sales-tracker"), "same app, same tile"
    assert first != thumbs.tile("ops-console"), "different apps differ"
    assert first["monogram"] == "ST"
    assert thumbs.tile("ops-console")["monogram"] == "OC"
    assert thumbs.tile("dashboard")["monogram"] == "D"


def test_a_captured_picture_is_served_and_shown_on_the_card(client):
    from launcher import thumbs
    upload(client, name="sales-dashboard")

    thumbs.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    # A one-pixel PNG is enough to prove the plumbing.
    thumbs.thumb_path("sales-dashboard").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    )

    response = client.get("/app/sales-dashboard/thumb")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"

    page = client.get("/").text
    assert "/app/sales-dashboard/thumb" in page
    assert 'class="app-shot-tile"' not in page, "a real picture replaces the fallback"


def test_deleting_an_app_takes_its_picture_with_it(client):
    from launcher import thumbs
    upload(client, name="sales-dashboard")
    thumbs.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    thumbs.thumb_path("sales-dashboard").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert thumbs.has_thumb("sales-dashboard")

    client.post("/app/sales-dashboard/delete", follow_redirects=False)

    assert not thumbs.has_thumb("sales-dashboard"), (
        "a deleted app must not leave its picture behind for the next app "
        "that happens to take the same name"
    )


def test_a_capture_failure_never_breaks_anything(client, monkeypatch):
    """The picture is decoration. A browser that will not start must cost a
    picture and nothing else."""
    from launcher import thumbs
    upload(client, name="sales-dashboard")

    monkeypatch.setattr(thumbs, "browser_path", lambda: None)
    assert thumbs.capture("sales-dashboard", "http://127.0.0.1:1/") is False

    response = client.post("/app/sales-dashboard/thumb/refresh",
                           follow_redirects=False)
    assert response.status_code == 303, "it redirects rather than erroring"
    assert client.get("/").status_code == 200, "the dashboard still renders"
