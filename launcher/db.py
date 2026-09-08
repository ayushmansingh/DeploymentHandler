"""SQLite persistence.

A connection is opened per operation rather than shared: the build worker runs
on a separate thread from the web handlers, and short-lived connections in WAL
mode are simpler to reason about than a shared connection plus locking.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    owner         TEXT    NOT NULL DEFAULT '',
    kind          TEXT    NOT NULL DEFAULT 'unknown',
    status        TEXT    NOT NULL DEFAULT 'new',
    host_port     INTEGER,
    container_id  TEXT,
    image_tag     TEXT,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS deploys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id        INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    zip_path      TEXT    NOT NULL,
    uploaded_by   TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'queued',
    error_summary TEXT,
    log_path      TEXT,
    created_at    REAL    NOT NULL,
    finished_at   REAL
);

-- The UNIQUE constraint on port is what makes allocation race-free: two
-- concurrent deploys cannot both claim the same host port, because the second
-- INSERT fails instead of silently overwriting.
CREATE TABLE IF NOT EXISTS ports (
    port         INTEGER PRIMARY KEY,
    app_id       INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    allocated_at REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deploys_app ON deploys(app_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ports_app ON ports(app_id);
"""


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def create_app(name: str, owner: str = "", db_path: Path | None = None) -> int:
    now = time.time()
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO apps (name, owner, created_at, updated_at) VALUES (?,?,?,?)",
            (name, owner, now, now),
        )
        return int(cur.lastrowid)


def get_app_by_name(name: str, db_path: Path | None = None) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM apps WHERE name = ?", (name,)).fetchone()


def get_app(app_id: int, db_path: Path | None = None) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()


def list_apps(db_path: Path | None = None) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM apps ORDER BY name").fetchall()


def update_app(app_id: int, db_path: Path | None = None, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"kind", "status", "host_port", "container_id", "image_tag", "owner"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update unknown app columns: {sorted(bad)}")
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE apps SET {assignments} WHERE id = ?",
            (*fields.values(), app_id),
        )


def delete_app(app_id: int, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))


def create_deploy(
    app_id: int,
    zip_path: str,
    uploaded_by: str = "",
    log_path: str | None = None,
    db_path: Path | None = None,
) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO deploys (app_id, zip_path, uploaded_by, log_path, created_at)"
            " VALUES (?,?,?,?,?)",
            (app_id, zip_path, uploaded_by, log_path, time.time()),
        )
        return int(cur.lastrowid)


def finish_deploy(
    deploy_id: int,
    status: str,
    error_summary: str | None = None,
    db_path: Path | None = None,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE deploys SET status = ?, error_summary = ?, finished_at = ?"
            " WHERE id = ?",
            (status, error_summary, time.time(), deploy_id),
        )


def set_deploy_status(deploy_id: int, status: str, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE deploys SET status = ? WHERE id = ?", (status, deploy_id))


def get_deploy(deploy_id: int, db_path: Path | None = None) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM deploys WHERE id = ?", (deploy_id,)).fetchone()


def list_deploys(app_id: int, limit: int = 20, db_path: Path | None = None) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM deploys WHERE app_id = ? ORDER BY created_at DESC LIMIT ?",
            (app_id, limit),
        ).fetchall()
