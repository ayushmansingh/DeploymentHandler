"""The dashboard behind a password, and the things that must stay open."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def locked(data_dir, monkeypatch):
    """A launcher with a password set, and a client that does not know it."""
    from launcher import app as app_module, auth, config, db

    monkeypatch.setattr(config, "PASSWORD", "Server123")
    monkeypatch.setattr(auth, "_secret_cache", None, raising=False)
    db.init()
    return TestClient(app_module.app, follow_redirects=False)


def test_the_dashboard_asks_for_the_password(locked):
    for path in ("/", "/deploy", "/admin/update"):
        response = locked.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path


def test_healthz_stays_open(locked):
    """The update helper polls this to decide whether a new version came
    back. Behind the password, every self-update would look like a failure
    and roll itself back."""
    response = locked.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_the_right_password_gets_you_in_and_stays_in(locked):
    response = locked.post("/login", data={"password": "Server123", "next": "/"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    cookie = response.cookies.get("launcher_session")
    assert cookie, "a session cookie is set"

    locked.cookies.set("launcher_session", cookie)
    assert locked.get("/").status_code == 200, "and the dashboard opens"


def test_the_wrong_password_does_not(locked):
    response = locked.post("/login", data={"password": "hunter2", "next": "/"})
    assert response.status_code == 303
    assert "bad=1" in response.headers["location"]
    assert not response.cookies.get("launcher_session")


def test_a_forged_cookie_is_refused(locked):
    """The cookie is signed with a secret kept out of the source, so a copy
    of the ZIP is not enough to mint one."""
    from launcher import auth

    locked.cookies.set("launcher_session", "99999999999.deadbeef")
    assert locked.get("/").status_code == 303

    # And the payload cannot be edited to extend it.
    real = auth.issue()
    payload, _, signature = real.partition(".")
    locked.cookies.set("launcher_session", f"{int(payload) + 86400}.{signature}")
    assert locked.get("/").status_code == 303


def test_an_expired_session_is_refused(locked, monkeypatch):
    from launcher import auth
    token = auth.issue()
    monkeypatch.setattr(auth.time, "time",
                        lambda: 9_999_999_999)   # long after it lapses
    assert auth.valid(token) is False


def test_the_password_cannot_bounce_you_to_another_site(locked):
    """`next` comes from the URL, so it must only ever point back here."""
    for hostile in ("https://evil.example/steal", "//evil.example/steal"):
        response = locked.post("/login", data={"password": "Server123",
                                               "next": hostile})
        assert response.headers["location"] == "/", hostile


def test_an_api_caller_is_told_rather_than_redirected(locked):
    response = locked.get("/api/apps")
    assert response.status_code == 401
    assert response.json()["detail"] == "Password required"


def test_signing_out_ends_the_session(locked):
    response = locked.post("/login", data={"password": "Server123", "next": "/"})
    locked.cookies.set("launcher_session", response.cookies["launcher_session"])
    assert locked.get("/").status_code == 200

    locked.post("/logout")
    locked.cookies.clear()
    assert locked.get("/").status_code == 303


def test_no_password_means_no_gate(data_dir, monkeypatch):
    """A server that has not set one must behave exactly as before."""
    from launcher import app as app_module, config, db

    monkeypatch.setattr(config, "PASSWORD", "")
    db.init()
    client = TestClient(app_module.app, follow_redirects=False)

    assert client.get("/").status_code == 200
    assert "Sign out" not in client.get("/").text, (
        "and no control that would do nothing"
    )
