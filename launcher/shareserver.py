"""The only part of the launcher that anything outside the office can reach.

It is a separate ASGI application on purpose. Not a route on the dashboard,
not a flag on a middleware - a different app object, with a different set of
routes, that does not import a template, a form or an admin handler. Whatever
goes wrong in it, there is no code path from here to Stop, Delete or Update,
because those handlers are not in this application at all.

It binds loopback only. The tunnel runs on this same machine and reaches it at
127.0.0.1; nothing on the office network can, so turning sharing on for one
app does not also widen what the rest of the building can see.

How a visitor gets in:

    GET /?k=<token>   ->  sets a cookie, redirects to /
    anything else     ->  proxied to that app, path untouched

The token is spent once, on the first request, and then lives in a cookie.
That is what keeps the app's own paths working: it sees "/" and "/api/x" and
"/assets/app.js" exactly as it would on the office network, so nothing has to
be rewritten and no app needs to know it is being shared. It also gets the
secret out of the address bar, out of the browser's history and out of any
Referer header the first page would otherwise send.

Revoking clears the token in the database. The cookie in somebody's browser
does not have to expire for that to bite: every request looks the token up
again, so the link dies on the next click.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import config, db

COOKIE = "launcher_share"

# Long enough that somebody sent a link in March is not locked out in April,
# and irrelevant to revocation, which does not wait for it.
COOKIE_DAYS = 30

# Same single-hop headers the per-app front server drops.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

_NO_LINK = """<!doctype html><meta charset="utf-8">
<title>Link not valid</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
         margin: 0; display: grid; place-items: center; min-height: 100vh;
         background: light-dark(#f9f9f7, #0d0d0d);
         color: light-dark(#0b0b0b, #ffffff); }
  div { max-width: 460px; padding: 32px; text-align: center; }
  p { color: light-dark(#52514e, #c3c2b7); }
</style>
<div>
  <h1 style="font-size:20px;margin:0 0 10px">This link is not valid</h1>
  <p>It may have been turned off, or the address may be incomplete. Ask
     whoever sent it to you for a new one.</p>
</div>"""


def _forwardable(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


def _refuse() -> HTMLResponse:
    """The same answer for a revoked link, a typo and a guess.

    Saying which it was would tell somebody probing whether a token was once
    real, and 404 against an app that does exist is still an answer.
    """
    return HTMLResponse(_NO_LINK, status_code=404)


def build_app(lookup=None, client: httpx.AsyncClient | None = None) -> Starlette:
    """`lookup` and `client` are injectable so a test can drive the real
    proxy path - headers, cookie stripping and all - without a live app or a
    live database behind it. Stubbing the proxy out instead would leave
    exactly the parts worth checking untested."""
    find = lookup or db.get_app_by_share_token
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=10.0), follow_redirects=False
    )

    async def handle(request: Request) -> Response:
        # A token in the query is a visitor arriving for the first time. It is
        # checked before anything is set, so a wrong one leaves no cookie.
        arriving = request.query_params.get("k", "")
        if arriving:
            if find(arriving) is None:
                return _refuse()
            response = RedirectResponse(request.url.path or "/", status_code=303)
            response.set_cookie(
                COOKIE, arriving,
                max_age=COOKIE_DAYS * 86400,
                httponly=True,     # not readable by the app's own JavaScript
                samesite="lax",
                path="/",
            )
            _harden(response)
            return response

        row = find(request.cookies.get(COOKIE, ""))
        if row is None:
            return _refuse()

        port = row["host_port"]
        if not port:
            return HTMLResponse(
                _NO_LINK.replace("This link is not valid",
                                 "This app is not running"),
                status_code=503,
            )

        return await _proxy(client, request, int(port))

    @asynccontextmanager
    async def lifespan(_):
        yield
        await client.aclose()

    return Starlette(
        routes=[Route("/{path:path}", handle, methods=METHODS)],
        lifespan=lifespan,
    )


def _harden(response: Response) -> None:
    """Headers a page reachable from the internet should carry.

    A shared link is secret, not secured, so the one thing worth insisting on
    is that it does not end up somewhere it can be found: not in a search
    index, and not in the Referer header of anything the page links out to.
    """
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"


async def _proxy(client: httpx.AsyncClient, request: Request, port: int) -> Response:
    """Hand the request to the app's own front server, unchanged.

    The app is reached at the port it already serves on, so sharing adds no
    second copy of anything and an app that is restarted or redeployed keeps
    working through the same link.
    """
    query = request.url.query
    url = httpx.URL(
        f"http://127.0.0.1:{port}",
        path=request.url.path or "/",
        query=query.encode() if query else None,
    )
    body = await request.body() if request.method not in ("GET", "HEAD") else None

    # The share cookie is ours, not the app's, and an app has no business
    # reading it - or being confused by it.
    headers = _forwardable(request.headers)
    headers.pop("cookie", None)
    remaining = "; ".join(
        f"{k}={v}" for k, v in request.cookies.items() if k != COOKIE
    )
    if remaining:
        headers["cookie"] = remaining

    upstream = client.build_request(request.method, url, headers=headers, content=body)
    try:
        response = await client.send(upstream, stream=True)
    except httpx.RequestError:
        return HTMLResponse(
            _NO_LINK.replace("This link is not valid", "This app is not responding"),
            status_code=502,
        )

    from starlette.background import BackgroundTask

    out = StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers=_forwardable(response.headers),
        background=BackgroundTask(response.aclose),
    )
    _harden(out)
    return out


def serve_in_background() -> None:
    """Run the share server beside the dashboard, if a port is configured.

    A thread rather than a process: the boundary that matters is the
    application object, which has no dashboard routes in it, and a thread
    keeps this out of the supervisor's pid bookkeeping entirely.
    """
    if not config.sharing_configured():
        return

    import threading

    import uvicorn

    def run() -> None:
        uvicorn.run(
            build_app(),
            host="127.0.0.1",          # never the network; the tunnel reaches us here
            port=config.SHARE_PORT,
            log_level="warning",
            access_log=False,
        )

    threading.Thread(target=run, name="share-server", daemon=True).start()
