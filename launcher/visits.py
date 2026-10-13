"""Counting how often each app gets opened.

This lives in the app's own front server, which is the only place that sees
*every* visit. Counting clicks on the dashboard instead would have missed the
larger half: once somebody has an app's link they bookmark it, paste it into a
chat, or put it on a wiki, and none of those ever touch the launcher again.
Here, a bookmark and a click from the dashboard are the same request.

What is kept is a number per app per day. No address, no name, no session, no
row per visit - there is nothing stored that could say who opened anything,
because the question being answered is "is this app still earning its place",
which never needed that.

Two further decisions worth knowing:

Opens, not requests. One dashboard loading sixty assets is one open, not
sixty-one. A browser says which is which with Sec-Fetch-Mode; where that is
missing the shape of the path decides.

Counts are held in memory and written every half minute. An app serving a
burst of traffic must not do a database write per page view, and losing the
last few seconds of counts when a process is stopped costs nothing that
matters here.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

# Paths that are part of a page rather than a visit to one.
_ASSET_SUFFIXES = (
    ".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".json", ".txt", ".wasm", ".mp4", ".webm",
)

FLUSH_SECONDS = 30


def is_an_open(method: str, path: str, headers) -> bool:
    """Whether this request is a person arriving, rather than a page's parts.

    Sec-Fetch-Mode is the browser telling us outright, and every browser that
    reaches this server sends it. The suffix check is for everything else:
    an old browser, a curl, a health check with no headers at all.
    """
    if method not in ("GET", "HEAD"):
        return False

    mode = headers.get("sec-fetch-mode", "")
    if mode:
        # "navigate" is a page the person is looking at. Anything else -
        # cors, no-cors, same-origin - is the page fetching its own parts.
        return mode == "navigate"

    if path.startswith("/api/"):
        return False
    tail = path.rsplit("/", 1)[-1].lower()
    return not tail.endswith(_ASSET_SUFFIXES)


def came_from_launcher(referer: str, launcher_port: int) -> bool:
    """Whether the visitor clicked through from the dashboard.

    Decided on the port, because the launcher answers to a name, to several
    addresses and to localhost, and the port is the one part of that which is
    the same for all of them. No Referer at all - a bookmark, a pasted link, a
    browser set to send none - counts as arriving directly, which is the
    honest reading of not knowing.
    """
    if not referer or not launcher_port:
        return False
    try:
        return urlsplit(referer).port == launcher_port
    except ValueError:         # a malformed Referer is somebody else's bug
        return False


class Counter:
    """Tallies in memory, written out on a timer.

    Safe to call from request handlers on any thread; the lock is held only
    long enough to add to an integer.
    """

    def __init__(self, app_id: int, flush_seconds: int = FLUSH_SECONDS,
                 writer=None, clock=time.time) -> None:
        self.app_id = app_id
        self.flush_seconds = flush_seconds
        self.clock = clock
        self._writer = writer
        self._lock = threading.Lock()
        self._pending: dict[str, list[int]] = {}
        self._last_flush = clock()
        self._last_open: float = 0.0

    def _write(self, day: str, opens: int, from_launcher: int, when: float) -> None:
        if self._writer is not None:
            self._writer(self.app_id, day, opens, from_launcher, when)
            return
        from . import db
        db.record_opens(self.app_id, day, opens, from_launcher, when)

    def record(self, from_launcher: bool = False) -> None:
        now = self.clock()
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        with self._lock:
            tally = self._pending.setdefault(day, [0, 0])
            tally[0] += 1
            tally[1] += 1 if from_launcher else 0
            self._last_open = now
        if now - self._last_flush >= self.flush_seconds:
            self.flush()

    def flush(self) -> None:
        """Write what has accumulated. Never raises.

        A database that is briefly locked, or a disk that is full, must not
        turn into a failed page load for somebody just using the app.
        """
        with self._lock:
            pending, self._pending = self._pending, {}
            when = self._last_open or self.clock()
            self._last_flush = self.clock()
        for day, (opens, from_launcher) in sorted(pending.items()):
            try:
                self._write(day, opens, from_launcher, when)
            except Exception:  # noqa: BLE001 - counting is never worth an outage
                pass
