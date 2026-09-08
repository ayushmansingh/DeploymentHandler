"""Host port registry.

Two rules drive this module:

1. A port is allocated once per app and then *kept*. People bookmark these
   URLs, so a redeploy must not reshuffle them.
2. Allocation must survive concurrent deploys. A plain "find a free port then
   use it" scan has a race window between the check and the bind; here the
   UNIQUE constraint on ports.port is the arbiter, and the socket bind is a
   second check against ports held by processes outside our registry.
"""
from __future__ import annotations

import random
import socket
import sqlite3
import time
from pathlib import Path

from . import config, db


class NoPortsAvailable(RuntimeError):
    """Every port in the configured range is taken."""


def _is_bindable(port: int) -> bool:
    """True if nothing outside our registry currently holds this port.

    SO_REUSEADDR is deliberately NOT set: we want to know whether the port is
    genuinely free right now, not whether we could steal a TIME_WAIT slot.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def allocated_port(app_id: int, db_path: Path | None = None) -> int | None:
    with db.connect(db_path) as conn:
        row = conn.execute("SELECT port FROM ports WHERE app_id = ?", (app_id,)).fetchone()
    return int(row["port"]) if row else None


def allocate(app_id: int, db_path: Path | None = None) -> int:
    """Return this app's stable host port, allocating one on first call."""
    existing = allocated_port(app_id, db_path)
    if existing is not None:
        return existing

    span = config.PORT_RANGE_END - config.PORT_RANGE_START + 1
    if span <= 0:
        raise NoPortsAvailable("configured port range is empty")

    # Start at a random offset so repeated allocations don't all contend on the
    # low end of the range, then walk the whole span so we still find the last
    # free port when the range is nearly full.
    offset = random.randrange(span)
    for i in range(span):
        port = config.PORT_RANGE_START + (offset + i) % span
        if not _is_bindable(port):
            continue
        try:
            with db.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO ports (port, app_id, allocated_at) VALUES (?,?,?)",
                    (port, app_id, time.time()),
                )
        except sqlite3.IntegrityError:
            # Either another deploy just claimed this port (port PK), or this
            # app was allocated one concurrently (idx_ports_app). Re-check the
            # latter before continuing the scan.
            concurrent = allocated_port(app_id, db_path)
            if concurrent is not None:
                return concurrent
            continue
        return port

    raise NoPortsAvailable(
        f"no free port in {config.PORT_RANGE_START}-{config.PORT_RANGE_END}"
    )


def release(app_id: int, db_path: Path | None = None) -> None:
    with db.connect(db_path) as conn:
        conn.execute("DELETE FROM ports WHERE app_id = ?", (app_id,))


def in_use(db_path: Path | None = None) -> dict[int, int]:
    """Map of port -> app_id, for the dashboard and for debugging."""
    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT port, app_id FROM ports").fetchall()
    return {int(r["port"]): int(r["app_id"]) for r in rows}
