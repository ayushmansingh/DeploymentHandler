"""The per-app front server. Native mode's replacement for nginx.

One of these runs per deployed app. It binds the app's public port and:

    /          serves the built frontend, with single-page-app fallback
    /api/...   proxies to the app's Python backend on 127.0.0.1

It also counts how often the app is opened, because it is the one place every
visit passes through - a bookmark and a click from the dashboard arrive here
identically. See launcher/visits.py for what is and is not recorded.

Serving both from one origin is what lets the frontend use relative "/api/..."
calls with no per-app configuration - the same property the container layout
provided, reproduced without containers.

Run as its own process so that a crash or a memory leak here is contained to
one app rather than taking the launcher down:

    python -m launcher.appserver --port 24817 --static ./dist --backend-port 30412
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import visits

# Headers that describe a single hop and must not be forwarded verbatim.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

BACKEND_UNREACHABLE = (
    "This app's backend is not responding.\n\n"
    "It may still be starting up - wait a few seconds and refresh. If this "
    "persists, open the app's page in the App Launcher and check the log."
)


def _forwardable(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


def build_app(static_dir: Path | None, backend_port: int | None,
              counter=None, launcher_port: int = 0) -> Starlette:
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{backend_port}" if backend_port else "http://127.0.0.1",
        timeout=httpx.Timeout(300.0, connect=10.0),
        follow_redirects=False,
    )

    async def proxy(request: Request) -> Response:
        if not backend_port:
            return PlainTextResponse("This app has no backend.", status_code=404)

        # An empty query still produces a trailing "?" if passed through, and
        # some routers treat "/docs?" as a different path from "/docs".
        query = request.url.query
        url = httpx.URL(path=request.url.path, query=query.encode() if query else None)

        # Read the body rather than streaming it: forwarding a stream makes
        # httpx use chunked encoding, which not every backend accepts, and
        # buffering lets httpx set an accurate Content-Length. This mirrors
        # nginx's default proxy_request_buffering.
        body = await request.body() if request.method not in ("GET", "HEAD") else None

        upstream = client.build_request(
            request.method,
            url,
            headers=_forwardable(request.headers),
            content=body,
        )
        try:
            response = await client.send(upstream, stream=True)
        except httpx.RequestError:
            return PlainTextResponse(BACKEND_UNREACHABLE, status_code=502)

        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=_forwardable(response.headers),
            background=_closing(response),
        )

    routes: list = [Route("/api/{path:path}", proxy, methods=[
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
    ])]

    if static_dir is not None:
        routes.append(Mount("/", app=StaticFiles(directory=static_dir, html=True)))
    elif backend_port:
        # Backend-only app: everything that is not /api goes upstream too, so
        # routes like /docs or /healthz still work.
        routes.append(Route("/{path:path}", proxy, methods=[
            "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
        ]))

    @asynccontextmanager
    async def lifespan(_):
        yield
        await client.aclose()

    app = Starlette(routes=routes, lifespan=lifespan)
    if static_dir is not None:
        app.add_middleware(SpaFallback, index=static_dir / "index.html")
    if counter is not None:
        # Outermost, so it sees the request whatever happens further in - an
        # open still counts when the backend is down and the person gets a
        # 502, because they still came looking.
        app.add_middleware(CountOpens, counter=counter, launcher_port=launcher_port)
    return app


class CountOpens(BaseHTTPMiddleware):
    """Tally page opens. Never the reason a request fails."""

    def __init__(self, app, counter, launcher_port: int) -> None:
        super().__init__(app)
        self.counter = counter
        self.launcher_port = launcher_port

    async def dispatch(self, request: Request, call_next):
        try:
            if visits.is_an_open(request.method, request.url.path, request.headers):
                self.counter.record(visits.came_from_launcher(
                    request.headers.get("referer", ""), self.launcher_port))
        except Exception:  # noqa: BLE001 - a counter must never break an app
            pass
        return await call_next(request)


def _closing(response: httpx.Response):
    from starlette.background import BackgroundTask

    return BackgroundTask(response.aclose)


class SpaFallback(BaseHTTPMiddleware):
    """Serve index.html for unmatched GETs so deep links survive a refresh.

    A single-page app owns its own routing, so /reports is a valid URL to the
    app even though no such file exists on disk.
    """

    def __init__(self, app, index: Path) -> None:
        super().__init__(app)
        self.index = index

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if (
            response.status_code == 404
            and request.method in ("GET", "HEAD")
            and not request.url.path.startswith("/api/")
            and self.index.is_file()
        ):
            return FileResponse(self.index)
        return response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--static", default="", help="directory of built frontend files")
    parser.add_argument("--backend-port", type=int, default=0)
    parser.add_argument("--app-id", type=int, default=0,
                        help="count opens for this app; 0 disables counting")
    parser.add_argument("--launcher-port", type=int, default=0,
                        help="used only to tell a click from the dashboard "
                             "apart from a bookmark")
    args = parser.parse_args()

    static_dir = Path(args.static).resolve() if args.static else None
    if static_dir is not None and not static_dir.is_dir():
        raise SystemExit(f"static directory does not exist: {static_dir}")

    counter = None
    if args.app_id:
        from .visits import Counter
        counter = Counter(args.app_id)

    import uvicorn

    uvicorn.run(
        build_app(static_dir, args.backend_port or None,
                  counter=counter, launcher_port=args.launcher_port),
        host="0.0.0.0",
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
