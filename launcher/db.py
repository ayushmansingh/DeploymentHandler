"""SQLite persistence.

A connection is opened per operation rather than shared: the build worker runs
on a separate thread from the web handlers, and short-lived connections in WAL
mode are simpler to reason about than a shared connection plus locking.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

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
    -- What the current version says it needs, as JSON. Names and descriptions
    -- only; the values live in app_settings.
    declared_settings TEXT NOT NULL DEFAULT '[]',
    -- The last time anybody opened this app, to the second. The daily rows
    -- in app_visits cannot answer "has anyone touched it this week?" on their
    -- own, and that is the question that retires a dead app.
    last_open_at  REAL,
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

-- Which groups an app is filed under on the dashboard. A row per group
-- rather than a list in a column, because an app can be in more than one and
-- the grid asks the question the other way round ("what is in Tools?").
--
-- The group name is stored as text, not as an id into a table of groups, so
-- that editing the configured list cannot orphan anything: a name no longer
-- offered is simply not shown, and comes back if the group is restored.
CREATE TABLE IF NOT EXISTS app_groups (
    app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    name   TEXT    NOT NULL,
    PRIMARY KEY (app_id, name)
);

-- How often each app was opened, by day. One row per app per day, not one
-- per visit: nobody is identified, nothing is kept that could identify them,
-- and 14 apps over 90 days is about 1300 rows rather than a growing log.
--
-- `opens` counts page opens, not requests - a dashboard pulling 60 assets is
-- one open. `from_launcher` is the subset that arrived by clicking through
-- the dashboard rather than from a bookmark or a pasted link.
CREATE TABLE IF NOT EXISTS app_visits (
    app_id        INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    day           TEXT    NOT NULL,
    opens         INTEGER NOT NULL DEFAULT 0,
    from_launcher INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (app_id, day)
);

CREATE INDEX IF NOT EXISTS idx_app_visits_day ON app_visits(day);
CREATE INDEX IF NOT EXISTS idx_deploys_app ON deploys(app_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_app_groups_name ON app_groups(name);
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
        "last_open_at": "REAL",
        "backend_port": "INTEGER",
        "pid": "INTEGER",
        "front_pid": "INTEGER",
        "declared_settings": "TEXT NOT NULL DEFAULT '[]'",
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
        "container_id", "image_tag", "owner", "declared_settings",
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


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

def record_opens(app_id: int, day: str, opens: int, from_launcher: int = 0,
                 when: float | None = None, db_path: Path | None = None) -> None:
    """Add to one app's tally for one day.

    Adding rather than setting, because the counts arrive in batches from a
    process that has been holding them in memory - two flushes on the same day
    have to accumulate, not overwrite each other.
    """
    if opens <= 0:
        return
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO app_visits (app_id, day, opens, from_launcher)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(app_id, day) DO UPDATE SET"
            "   opens = opens + excluded.opens,"
            "   from_launcher = from_launcher + excluded.from_launcher",
            (app_id, day, opens, from_launcher),
        )
        conn.execute(
            "UPDATE apps SET last_open_at = MAX(COALESCE(last_open_at, 0), ?)"
            " WHERE id = ?",
            (when if when is not None else time.time(), app_id),
        )


def opens_by_app(since_day: str, db_path: Path | None = None) -> dict[int, dict]:
    """Totals per app since a day, inclusive."""
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT app_id, SUM(opens) AS opens,"
            "       SUM(from_launcher) AS from_launcher"
            " FROM app_visits WHERE day >= ? GROUP BY app_id",
            (since_day,),
        ).fetchall()
    return {
        int(r["app_id"]): {"opens": int(r["opens"] or 0),
                           "from_launcher": int(r["from_launcher"] or 0)}
        for r in rows
    }


def daily_opens(since_day: str, app_id: int | None = None,
                db_path: Path | None = None) -> dict[str, int]:
    """Opens per day, for the whole server or for one app.

    Days with no visits are absent rather than zero; the caller fills the gaps,
    because only it knows which days the chart is meant to cover.
    """
    query = ("SELECT day, SUM(opens) AS opens FROM app_visits"
             " WHERE day >= ?")
    params: list = [since_day]
    if app_id is not None:
        query += " AND app_id = ?"
        params.append(app_id)
    query += " GROUP BY day ORDER BY day"
    with connect(db_path) as conn:
        return {r["day"]: int(r["opens"] or 0)
                for r in conn.execute(query, params).fetchall()}


def prune_visits(before_day: str, db_path: Path | None = None) -> int:
    """Drop counts older than the window the Stats tab shows."""
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM app_visits WHERE day < ?", (before_day,))
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

def set_app_groups(
    app_id: int, names: Iterable[str], db_path: Path | None = None
) -> None:
    """Replace an app's groups with exactly this set.

    Replacing rather than merging is what lets the form send the boxes that
    are ticked and nothing else: unticking the last one has to be able to
    leave an app with no group at all.
    """
    wanted = sorted({name.strip() for name in names if name and name.strip()})
    with connect(db_path) as conn:
        conn.execute("DELETE FROM app_groups WHERE app_id = ?", (app_id,))
        conn.executemany(
            "INSERT INTO app_groups (app_id, name) VALUES (?,?)",
            [(app_id, name) for name in wanted],
        )
        conn.execute("UPDATE apps SET updated_at = ? WHERE id = ?",
                     (time.time(), app_id))


def app_groups(app_id: int, db_path: Path | None = None) -> list[str]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM app_groups WHERE app_id = ? ORDER BY name", (app_id,)
        ).fetchall()
    return [row["name"] for row in rows]


def groups_by_app(db_path: Path | None = None) -> dict[int, list[str]]:
    """Every app's groups in one query.

    The dashboard draws every card at once and polls itself, so asking per app
    would be one round trip per app per refresh for a value this small.
    """
    found: dict[int, list[str]] = {}
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT app_id, name FROM app_groups ORDER BY app_id, name"
        ).fetchall()
    for row in rows:
        found.setdefault(int(row["app_id"]), []).append(row["name"])
    return found


def set_declared_settings(
    app_id: int, declared: list[dict], db_path: Path | None = None
) -> None:
    """Record what the version now on disk needs."""
    update_app(app_id, db_path=db_path, declared_settings=json.dumps(declared))


def get_declared_settings(app_id: int, db_path: Path | None = None) -> list[dict]:
    row = get_app(app_id, db_path)
    if row is None:
        return []
    try:
        declared = json.loads(row["declared_settings"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return declared if isinstance(declared, list) else []


def unset_settings(app_id: int, db_path: Path | None = None) -> list[dict]:
    """Every declared setting still waiting on a person, required or not.

    Optional ones are listed so nobody has to read the source to find out they
    exist; they simply do not hold the app back. Settings with a default are
    not here at all - the app answered its own question, and they are shown
    with their value instead of asked for.
    """
    have = settings_env(app_id, db_path)
    return [
        d for d in get_declared_settings(app_id, db_path)
        if d.get("name") not in have and not d.get("default")
    ]


def missing_settings(app_id: int, db_path: Path | None = None) -> list[dict]:
    """The unset settings that actually stop the app starting.

    A declaration is required unless it says otherwise, so an older record
    without the field still behaves as it did.
    """
    return [d for d in unset_settings(app_id, db_path) if d.get("required", True)]


def declared_defaults(app_id: int, db_path: Path | None = None) -> dict[str, str]:
    """The values the app brought with it, before anyone overrode them."""
    return {
        d["name"]: str(d["default"])
        for d in get_declared_settings(app_id, db_path)
        if d.get("name") and d.get("default")
    }


def effective_env(app_id: int, db_path: Path | None = None) -> dict[str, str]:
    """What the app actually runs with: its own defaults, then any overrides.

    Stored values are applied last, so typing one into the dashboard replaces
    the default and deleting it puts the default back.
    """
    env = declared_defaults(app_id, db_path)
    env.update(settings_env(app_id, db_path))
    return env


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
