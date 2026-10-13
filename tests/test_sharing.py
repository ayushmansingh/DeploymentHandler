"""Sharing one app with somebody outside the office.

The whole of the access control is a secret in a link, so what is checked
here is mostly the ways that secret could fail to be the only way in: a
missing cookie, a revoked token, a guess, and - the one that would matter
most - whether the dashboard is reachable from the same door.
"""
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from launcher import config, db, shareserver


@pytest.fixture
def client(data_dir, monkeypatch):
    from launcher import app as app_module

    monkeypatch.setattr(config, "PASSWORD", "")
    monkeypatch.setattr(config, "SHARE_PORT", 9443)
    monkeypatch.setattr(config, "SHARE_BASE_URL", "https://share.example.com")
    db.init()
    return TestClient(app_module.app, follow_redirects=False)


def make(name: str = "revenue-explorer", port: int = 24801) -> int:
    app_id = db.create_app(name, owner="Priya")
    db.update_app(app_id, status="live", host_port=port, backend_port=30401)
    return app_id


def token_of(name: str) -> str:
    return db.get_app_by_name(name)["share_token"]


# ---------------------------------------------------------------------------
# Making and revoking a link
# ---------------------------------------------------------------------------

def test_an_app_is_not_shared_until_somebody_shares_it(client):
    """Nothing leaves the building by default, or by upgrading."""
    make()
    assert token_of("revenue-explorer") is None


def test_sharing_mints_a_secret_long_enough_to_be_one(client):
    make()

    client.post("/app/revenue-explorer/share", data={"shared_by": "Priya"})

    token = token_of("revenue-explorer")
    assert token and len(token) >= 32


def test_two_apps_never_get_the_same_link(client):
    make("revenue-explorer", 24801)
    make("zip-tool", 24802)

    client.post("/app/revenue-explorer/share")
    client.post("/app/zip-tool/share")

    assert token_of("revenue-explorer") != token_of("zip-tool")


def test_resharing_replaces_the_old_secret(client):
    """The reason to re-share is that the link reached the wrong person, so
    the old one has to stop working rather than both being live."""
    make()
    client.post("/app/revenue-explorer/share")
    first = token_of("revenue-explorer")

    client.post("/app/revenue-explorer/share")

    assert token_of("revenue-explorer") != first


def test_revoking_clears_it(client):
    make()
    client.post("/app/revenue-explorer/share")

    client.post("/app/revenue-explorer/share/revoke")

    assert token_of("revenue-explorer") is None


def test_the_page_prints_a_link_somebody_can_send(client):
    make()
    client.post("/app/revenue-explorer/share")

    body = client.get("/app/revenue-explorer").text

    assert f"https://share.example.com/?k={token_of('revenue-explorer')}" in body


def test_without_a_tunnel_address_it_says_so_rather_than_inventing_one(
        client, monkeypatch):
    monkeypatch.setattr(config, "SHARE_BASE_URL", "")
    make()
    client.post("/app/revenue-explorer/share")

    body = client.get("/app/revenue-explorer").text

    assert "LAUNCHER_SHARE_BASE_URL" in body
    assert "https://share.example.com" not in body


def test_the_dashboard_marks_which_apps_leave_the_building(client):
    make()
    assert "pill shared" not in client.get("/").text

    client.post("/app/revenue-explorer/share")

    assert "pill shared" in client.get("/").text


# ---------------------------------------------------------------------------
# The door itself
# ---------------------------------------------------------------------------

@pytest.fixture
def outside(data_dir):
    """The share server, with a stub app behind it.

    Nothing is proxied anywhere real: what matters here is who gets past the
    door, not what is on the other side of it.
    """
    db.init()
    reached: list[str] = []

    async def fake_proxy(client, request: Request, port: int):
        from starlette.responses import PlainTextResponse
        reached.append(f"{port}{request.url.path}")
        return PlainTextResponse("the app itself")

    import launcher.shareserver as module
    original = module._proxy
    module._proxy = fake_proxy
    app = module.build_app()
    client = TestClient(app, follow_redirects=False)
    client.reached = reached  # type: ignore[attr-defined]
    yield client
    module._proxy = original


def test_no_token_gets_nothing(outside):
    response = outside.get("/")
    assert response.status_code == 404
    assert "not valid" in response.text
    assert outside.reached == []


def test_a_guessed_token_gets_nothing_and_sets_no_cookie(outside):
    response = outside.get("/?k=obviously-wrong")

    assert response.status_code == 404
    assert not response.cookies.get(shareserver.COOKIE)
    assert outside.reached == []


def test_a_valid_link_sets_a_cookie_and_sends_you_to_the_app(outside):
    app_id = make()
    db.share_app(app_id, "secret-token-value", "Priya")

    first = outside.get("/?k=secret-token-value")

    assert first.status_code == 303
    assert first.headers["location"] == "/"
    assert first.cookies.get(shareserver.COOKIE) == "secret-token-value"


def test_the_secret_leaves_the_address_bar_immediately(outside):
    """It is spent on the first request. Leaving it in the URL would put it
    in the visitor's history and in the Referer of anything the page links."""
    app_id = make()
    db.share_app(app_id, "secret-token-value")

    response = outside.get("/?k=secret-token-value")

    assert "k=" not in response.headers["location"]
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_once_in_the_app_sees_its_own_paths_unchanged(outside):
    """This is why the token is a cookie and not a path prefix: an app that
    asks for /api/rows and /assets/app.js has to get exactly those."""
    app_id = make(port=24801)
    db.share_app(app_id, "secret-token-value")
    outside.cookies.set(shareserver.COOKIE, "secret-token-value")

    outside.get("/api/rows")
    outside.get("/assets/app.js")

    assert outside.reached == ["24801/api/rows", "24801/assets/app.js"]


def test_revoking_locks_out_a_browser_that_already_has_the_cookie(outside):
    """Revocation cannot wait for a cookie to expire - the token is looked
    up again on every single request."""
    app_id = make()
    db.share_app(app_id, "secret-token-value")
    outside.cookies.set(shareserver.COOKIE, "secret-token-value")
    assert outside.get("/").status_code == 200

    db.unshare_app(app_id)

    assert outside.get("/").status_code == 404
    assert outside.reached == ["24801/"], "and nothing reached the app after it"


def test_an_empty_token_does_not_match_an_unshared_app(outside):
    """Unshared apps hold NULL. A blank cookie must not find one of them."""
    make()  # never shared

    outside.cookies.set(shareserver.COOKIE, "")

    assert outside.get("/").status_code == 404
    assert db.get_app_by_share_token("") is None


def test_a_shared_app_that_is_not_running_says_so(outside):
    app_id = make()
    db.update_app(app_id, host_port=None)
    db.share_app(app_id, "secret-token-value")
    outside.cookies.set(shareserver.COOKIE, "secret-token-value")

    response = outside.get("/")

    assert response.status_code == 503
    assert "not running" in response.text


def test_a_link_reaches_only_its_own_app(outside):
    """Holding one app's secret must not be a way into the other thirteen."""
    first = make("revenue-explorer", 24801)
    make("payroll", 24802)
    db.share_app(first, "secret-token-value")
    outside.cookies.set(shareserver.COOKIE, "secret-token-value")

    outside.get("/")

    assert outside.reached == ["24801/"]


# ---------------------------------------------------------------------------
# What is NOT behind that door
# ---------------------------------------------------------------------------

def test_the_share_server_has_no_dashboard_routes_at_all(outside):
    """The boundary is the application object, not a check somebody has to
    remember to write. There is no handler here that could serve /admin,
    /upload or /app/x/delete - they are in a different app entirely."""
    app_id = make()
    db.share_app(app_id, "secret-token-value")
    outside.cookies.set(shareserver.COOKIE, "secret-token-value")

    # With a valid cookie every path is proxied to the app, so none of these
    # reaches launcher code - they are just paths inside somebody's dashboard.
    for path in ("/admin/update", "/upload", "/app/payroll/delete", "/api/apps"):
        outside.get(path)

    assert all(hit.startswith("24801") for hit in outside.reached)


def test_the_share_server_imports_nothing_that_can_change_anything():
    """A regression guard with teeth: if somebody ever pulls the dashboard in
    here to "reuse" something, this fails.

    It reads the import statements rather than searching the text, so prose
    about the dashboard does not trip it and an actual import cannot hide in
    a comment either way.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(shareserver))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)

    for forbidden in ("deployer", "selfupdate", "auth", "supervisor",
                      "native", "archive", "templates"):
        assert forbidden not in imported, f"shareserver must not import {forbidden}"

    assert {"config", "db"} <= imported, "it does still need these two"


def test_it_listens_on_loopback_only(outside):
    """Binding the network instead would mean turning sharing on for one app
    also opened a second, unauthenticated door onto the office LAN."""
    import inspect

    source = inspect.getsource(shareserver.serve_in_background)
    assert 'host="127.0.0.1"' in source
    assert "0.0.0.0" not in source


def test_nothing_listens_until_a_port_is_configured(monkeypatch):
    monkeypatch.setattr(config, "SHARE_PORT", 0)
    started: list = []
    monkeypatch.setattr("threading.Thread", lambda **kw: started.append(kw))

    shareserver.serve_in_background()

    assert started == []


# ---------------------------------------------------------------------------
# What actually reaches the app, with the real proxy running
# ---------------------------------------------------------------------------

@pytest.fixture
def upstream():
    """A real HTTP server standing in for a deployed app.

    Over a real socket rather than a mocked transport, because the proxy
    streams its response - and a stub that hands back an already-read body
    cannot exercise that at all. It records what it was sent so the test can
    check what the app would actually have seen.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _record(self):
            length = int(self.headers.get("content-length") or 0)
            received.append({
                "path": self.path,
                "method": self.command,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": self.rfile.read(length) if length else b"",
            })
            body = b"the app itself"
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(body)))
            self.send_header("set-cookie", "app_session=xyz")
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _record

        def log_message(self, *args):  # keep the test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.received = received  # type: ignore[attr-defined]
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxied(data_dir, upstream):
    """The share server with its real proxy, pointed at that real app."""
    db.init()
    client = TestClient(shareserver.build_app(), follow_redirects=False)
    client.port = upstream.server_address[1]  # type: ignore[attr-defined]
    client.received = upstream.received       # type: ignore[attr-defined]
    return client


def shared_at_upstream(proxied, token: str = "secret-token-value") -> int:
    """An app whose recorded port really is the stub server's."""
    app_id = make(port=proxied.port)
    db.share_app(app_id, token)
    proxied.cookies.set(shareserver.COOKIE, token)
    return app_id


def test_the_request_arrives_at_the_apps_own_path_and_query(proxied):
    """The app has to see exactly what it would on the office network."""
    shared_at_upstream(proxied)

    proxied.get("/api/rows?page=2")

    assert proxied.received[0]["path"] == "/api/rows?page=2"


def test_the_app_never_receives_our_cookie(proxied):
    """It is ours, it is a credential, and an app has no business reading it
    - nor being confused by a cookie it did not set."""
    shared_at_upstream(proxied)
    proxied.cookies.set("app_preference", "dark")

    proxied.get("/")

    forwarded = proxied.received[0]["headers"].get("cookie", "")
    assert "secret-token-value" not in forwarded
    assert "app_preference=dark" in forwarded, "the app's own cookies still pass"


def test_the_apps_own_response_comes_back_intact(proxied):
    shared_at_upstream(proxied)

    response = proxied.get("/")

    assert response.status_code == 200
    assert response.text == "the app itself"
    assert "app_session=xyz" in response.headers.get("set-cookie", "")


def test_shared_pages_are_kept_out_of_search_engines(proxied):
    """A secret link that turns up in a search result is not a secret."""
    shared_at_upstream(proxied)

    response = proxied.get("/")

    assert "noindex" in response.headers["X-Robots-Tag"]
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_a_post_carries_its_body_through(proxied):
    shared_at_upstream(proxied)

    proxied.post("/api/save", json={"rows": 3})

    assert b'"rows"' in proxied.received[0]["body"]
    assert proxied.received[0]["method"] == "POST"
