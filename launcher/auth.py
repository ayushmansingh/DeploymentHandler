"""A password on the dashboard, so a mistake takes a deliberate step.

This is a latch, not a lock. The server speaks plain HTTP on the office LAN,
so the password crosses the network readable by anyone who can already watch
the traffic. What it does do is stop somebody who wandered into the dashboard
from stopping or deleting an app by accident, which is the thing that has
actually gone wrong.

Two things are deliberately left open. The apps themselves are served by
their own processes on their own ports and are not touched - every link
already shared with colleagues keeps working. And /healthz stays reachable,
because the update helper polls it to decide whether a new version came back;
gating it would make every self-update look like a failure and roll back.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from . import config

COOKIE = "launcher_session"

# Long enough that the team is not typing a password every morning, short
# enough that a browser left open on a shared desktop does not stay signed in
# for ever.
SESSION_DAYS = 14

# Where the signing secret lives. Kept out of the source so the cookie cannot
# be forged from a copy of the ZIP, and persisted so a restart does not sign
# everybody out.
_SECRET_FILE = config.DATA_DIR / "session.key"
_secret_cache: bytes | None = None


def required() -> bool:
    """Whether a password has been set at all."""
    return bool(config.PASSWORD)


def _secret() -> bytes:
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    try:
        _secret_cache = _SECRET_FILE.read_bytes()
        if _secret_cache:
            return _secret_cache
    except OSError:
        pass

    fresh = secrets.token_bytes(32)
    try:
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_bytes(fresh)
    except OSError:
        # An unwritable data directory is a bigger problem than this, and a
        # secret held only in memory still works until the next restart.
        pass
    _secret_cache = fresh
    return fresh


def check(password: str) -> bool:
    """Whether this is the password, compared without leaking its length."""
    if not required():
        return True
    return hmac.compare_digest(password or "", config.PASSWORD)


def issue() -> str:
    """A signed token saying this browser has typed the password."""
    expires = int(time.time()) + SESSION_DAYS * 86400
    payload = str(expires)
    signature = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def valid(token: str | None) -> bool:
    """Whether a cookie was issued here and has not expired."""
    if not token or "." not in token:
        return False
    payload, _, signature = token.partition(".")
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False
