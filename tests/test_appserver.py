"""The per-app front server: static files, SPA fallback, and the /api proxy."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from starlette.testclient import TestClient

from launcher import appserver


class _Backend(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's interface
        body = json.dumps({"path": self.path, "ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length)
        self.send_response(201)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def backend():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Backend)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def site(tmp_path):
    (tmp_path / "index.html").write_text("<h1>dashboard</h1>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    return tmp_path


def test_serves_the_built_frontend(site):
    with TestClient(appserver.build_app(site, None)) as client:
        assert client.get("/").text == "<h1>dashboard</h1>"
        assert client.get("/assets/app.js").status_code == 200


def test_deep_links_fall_back_to_index(site):
    """A single-page app owns its routing, so /reports must not 404."""
    with TestClient(appserver.build_app(site, None)) as client:
        response = client.get("/reports/2026")
        assert response.status_code == 200
        assert "dashboard" in response.text


def test_api_calls_reach_the_backend(site, backend):
    with TestClient(appserver.build_app(site, backend)) as client:
        response = client.get("/api/items?page=2")
        assert response.status_code == 200
        assert response.json()["path"] == "/api/items?page=2"


def test_request_bodies_are_forwarded(site, backend):
    with TestClient(appserver.build_app(site, backend)) as client:
        response = client.post("/api/items", content=b"payload-here")
        assert response.status_code == 201
        assert response.content == b"payload-here"


def test_api_paths_are_not_swallowed_by_the_spa_fallback(site, backend):
    """A 404 from the backend must stay a 404, not become the dashboard."""
    with TestClient(appserver.build_app(site, None)) as client:
        response = client.get("/api/missing")
        assert response.status_code == 404
        assert "dashboard" not in response.text


def test_backend_down_gives_a_plain_explanation(site):
    # Port 9 (discard) refuses connections, standing in for a dead backend.
    with TestClient(appserver.build_app(site, 9)) as client:
        response = client.get("/api/items")
        assert response.status_code == 502
        assert "not responding" in response.text


def test_backend_only_app_serves_every_route_from_the_backend(backend):
    with TestClient(appserver.build_app(None, backend)) as client:
        assert client.get("/api/items").json()["ok"] is True
        assert client.get("/docs").json()["path"] == "/docs"
