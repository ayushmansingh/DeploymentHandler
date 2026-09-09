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
