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
    backend_port  INTEGER,
    pid           INTEGER,
    front_pid     INTEGER,
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
-- concurrent deploys cannot both claim the same port, because the second
-- INSERT fails instead of silently overwriting.
--
-- An app holds one port per role: "public" is the one people browse to, and
-- in native mode "backend" is a loopback-only port its front server proxies
-- to. Both are kept so that a restart reuses them rather than reshuffling.
CREATE TABLE IF NOT EXISTS ports (
    port         INTEGER PRIMARY KEY,
    app_id       INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    role         TEXT    NOT NULL DEFAULT 'public',
    allocated_at REAL    NOT NULL
);

-- Per-app configuration, injected into the app's environment when it starts.
-- Values are write-only from the dashboard: they are set and replaced there,
-- never read back out, so a key someone pasted in cannot be retrieved by the
-- next person to open the page.
CREATE TABLE IF NOT EXISTS app_settings (
    app_id     INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    updated_at REAL    NOT NULL,
    updated_by TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (app_id, key)
);

CREATE INDEX IF NOT EXISTS idx_deploys_app ON deploys(app_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ports_app_role ON ports(app_id, role);
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


# Columns added after the first release. Applied on startup so an existing
# database picks them up without a manual migration step.
_ADDED_COLUMNS = {
    "apps": {
        "backend_port": "INTEGER",
        "pid": "INTEGER",
        "front_pid": "INTEGER",
    },
    "ports": {
        "role": "TEXT NOT NULL DEFAULT 'public'",
    },
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)


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
    allowed = {
        "kind", "status", "host_port", "backend_port", "pid", "front_pid",
        "container_id", "image_tag", "owner",
    }
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


def set_setting(
    app_id: int, key: str, value: str, updated_by: str = "",
    db_path: Path | None = None,
) -> None:
    """Store or replace one setting. Replacing is the only way to change it."""
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO app_settings (app_id, key, value, updated_at, updated_by)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(app_id, key) DO UPDATE SET"
            " value = excluded.value, updated_at = excluded.updated_at,"
            " updated_by = excluded.updated_by",
            (app_id, key, value, time.time(), updated_by),
        )


def list_setting_keys(app_id: int, db_path: Path | None = None) -> list[sqlite3.Row]:
    """Key names and when they changed - deliberately never the values.

    The dashboard renders this, so it must not be able to leak a secret even
    by accident.
    """
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT key, updated_at, updated_by FROM app_settings"
            " WHERE app_id = ? ORDER BY key",
            (app_id,),
        ).fetchall()


def settings_env(app_id: int, db_path: Path | None = None) -> dict[str, str]:
    """The settings as environment variables, for launching the app."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE app_id = ?", (app_id,)
        ).fetchall()
    return {row["key"]: row["value"] for row in rows}


def delete_setting(app_id: int, key: str, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM app_settings WHERE app_id = ? AND key = ?", (app_id, key)
        )


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


def list_unfinished_deploys(db_path: Path | None = None) -> list[sqlite3.Row]:
    """Deploys that were still running when the launcher last stopped."""
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM deploys WHERE status IN ('queued', 'building')"
        ).fetchall()


def get_deploy(deploy_id: int, db_path: Path | None = None) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM deploys WHERE id = ?", (deploy_id,)).fetchone()


def list_deploys(app_id: int, limit: int = 20, db_path: Path | None = None) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM deploys WHERE app_id = ? ORDER BY created_at DESC LIMIT ?",
            (app_id, limit),
        ).fetchall()
