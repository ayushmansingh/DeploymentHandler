"""The Server tab behind a password, and the whole rest of the app in front.

The password exists so nobody updates or restarts the launcher by accident.
It is deliberately NOT a login for the product: the team deploys, stops and
starts apps all day, and asking them to sign in for that would only teach
everybody the password.
"""
import pytest
from fastapi.testclient import TestClient

# Everything an ordinary user does. None of it may ever ask for a password.
OPEN_PAGES = ("/", "/deploy", "/partials/apps", "/api/apps", "/healthz")


@pytest.fixture
def locked(data_dir, monkeypatch):
    """A launcher with a password set, and a client that does not know it."""
    from launcher import app as app_module, auth, config, db

    monkeypatch.setattr(config, "PASSWORD", "Server123")
    monkeypatch.setattr(auth, "_secret_cache", None, raising=False)
    db.init()
    return TestClient(app_module.app, follow_redirects=False)


def test_the_server_tab_asks_for_the_password(locked):
    response = locked.get("/admin/update")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_applying_an_update_asks_too(locked):
    """The GET being gated is cosmetic if the POST behind it is not."""
    response = locked.post("/admin/update")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", OPEN_PAGES)
def test_the_rest_of_the_app_is_not_behind_the_password(locked, path):
    assert locked.get(path).status_code == 200, path


@pytest.mark.parametrize("path", [
    "/app/anything/stop", "/app/anything/start", "/app/anything/delete",
])
def test_the_everyday_app_buttons_are_not_gated(locked, path):
    """A missing app is a 404 from the handler. A 303 to /login would mean
    the middleware stopped it before the handler ever saw it."""
    response = locked.post(path)
    assert response.status_code != 303, path


def test_healthz_stays_open(locked):
    """The update helper polls this to decide whether a new version came
    back. Behind the password, every self-update would look like a failure
    and roll itself back."""
    response = locked.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_the_right_password_gets_you_in_and_stays_in(locked):
    response = locked.post(
        "/login", data={"password": "Server123", "next": "/admin/update"})
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/update"

    cookie = response.cookies.get("launcher_session")
    assert cookie, "a session cookie is set"

    locked.cookies.set("launcher_session", cookie)
    assert locked.get("/admin/update").status_code == 200


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
    assert locked.get("/admin/update").status_code == 303

    # And the payload cannot be edited to extend it.
    real = auth.issue()
    payload, _, signature = real.partition(".")
    locked.cookies.set("launcher_session", f"{int(payload) + 86400}.{signature}")
    assert locked.get("/admin/update").status_code == 303


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


def test_a_script_is_told_rather_than_redirected(locked):
    response = locked.get("/admin/update", headers={"accept": "application/json"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Password required"


def test_signing_out_ends_the_session(locked):
    response = locked.post(
        "/login", data={"password": "Server123", "next": "/admin/update"})
    locked.cookies.set("launcher_session", response.cookies["launcher_session"])
    assert locked.get("/admin/update").status_code == 200

    locked.post("/logout")
    locked.cookies.clear()
    assert locked.get("/admin/update").status_code == 303


def test_sign_out_is_offered_only_to_somebody_who_signed_in(locked):
    """Most people never sign in, because only one tab asks. A Sign out
    button on their dashboard would be a control that does nothing."""
    assert "Sign out" not in locked.get("/").text

    response = locked.post(
        "/login", data={"password": "Server123", "next": "/admin/update"})
    locked.cookies.set("launcher_session", response.cookies["launcher_session"])
    assert "Sign out" in locked.get("/").text


def test_no_password_means_no_gate(data_dir, monkeypatch):
    """A server that has not set one must behave exactly as before."""
    from launcher import app as app_module, config, db

    monkeypatch.setattr(config, "PASSWORD", "")
    db.init()
    client = TestClient(app_module.app, follow_redirects=False)

    assert client.get("/").status_code == 200
    assert client.get("/admin/update").status_code == 200
    assert "Sign out" not in client.get("/").text


def test_a_new_route_is_open_unless_somebody_guards_it():
    """The rule is a closed list of guarded paths, not a list of exceptions,
    so forgetting to think about a new route leaves it open rather than
    locking the team out of it."""
    from launcher import app as app_module

    assert app_module._is_guarded("/admin/update")
    assert app_module._is_guarded("/admin")
    for path in ("/", "/deploy", "/app/x/stop", "/administrivia", "/healthz"):
        assert not app_module._is_guarded(path), path
