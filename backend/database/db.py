"""SQLite persistence (stdlib sqlite3, WAL mode, one connection per thread)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from backend import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                 -- main | gameplay
    title TEXT,
    source_url TEXT,
    original_name TEXT,
    path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | downloading | ready | error
    info TEXT,
    error TEXT,
    usable_start REAL,
    usable_end REAL,
    trim_start REAL,        -- user trim (seconds) for long gameplay; NULL = no trim
    trim_end REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_kind ON media(kind);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',   -- draft | processing | ready | error | cancelled
    main_media_id TEXT,
    gameplay_media_ids TEXT NOT NULL DEFAULT '[]',
    settings TEXT NOT NULL DEFAULT '{}',
    timeline TEXT,
    transcript_path TEXT,
    transcript_language TEXT,
    warnings TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start_frame INTEGER NOT NULL,
    end_frame INTEGER NOT NULL,
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    duration REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',          -- pending | rendering | ready | error
    subtitle_status TEXT NOT NULL DEFAULT 'none',    -- none | auto | edited | empty | disabled
    cues TEXT NOT NULL DEFAULT '[]',
    overrides TEXT NOT NULL DEFAULT '{}',
    gameplay TEXT,
    auto_crop TEXT,
    video_path TEXT,
    thumb_path TEXT,
    verify TEXT,
    error TEXT,
    approved INTEGER NOT NULL DEFAULT 0,
    caption TEXT,
    hashtags TEXT,
    publish_status TEXT NOT NULL DEFAULT 'not_published',
    publish_info TEXT NOT NULL DEFAULT '{}',
    publish_mode TEXT NOT NULL DEFAULT 'manual',     -- manual | api_direct | api_inbox
    scheduled_at REAL,
    publish_order INTEGER,
    rendered_at REAL,
    updated_at REAL NOT NULL,
    UNIQUE(project_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_episodes_project ON episodes(project_id);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    lane TEXT NOT NULL DEFAULT 'render',
    project_id TEXT,
    media_id TEXT,
    episode_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed | cancelled
    stage TEXT,
    progress REAL NOT NULL DEFAULT 0,
    eta REAL,
    message TEXT,
    error TEXT,
    error_detail TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- Unsaved editing state (new-project form, episode editor), kept on disk so it survives restarts.
CREATE TABLE IF NOT EXISTS drafts (
    key TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    base TEXT,
    updated_at REAL NOT NULL
);
"""

JSON_FIELDS = {
    "media": {"info"},
    "projects": {"gameplay_media_ids", "settings", "timeline", "warnings"},
    "episodes": {"cues", "overrides", "gameplay", "auto_crop", "verify", "publish_info"},
    "jobs": {"payload"},
}

_local = threading.local()
_init_lock = threading.Lock()
_db_path: Path | None = None


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def now() -> float:
    return time.time()


def set_db_path(path: Path) -> None:
    global _db_path
    _db_path = path
    _local.__dict__.clear()


def _path() -> Path:
    return _db_path or config.DB_PATH


def connect() -> sqlite3.Connection:
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    path = str(_path())
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    _local.conn = conn
    _local.path = path
    return conn


# Columns added after the first release. Only ever append: an update must open every older database.
MIGRATIONS = [("episodes", "auto_crop", "TEXT"), ("media", "trim_start", "REAL"), ("media", "trim_end", "REAL"),
              ("media", "fetch_start", "REAL"), ("media", "fetch_end", "REAL"),
              ("episodes", "deleted", "INTEGER NOT NULL DEFAULT 0")]
# PRAGMA user_version: bump together with MIGRATIONS / new tables.
SCHEMA_VERSION = 4


def schema_version() -> int:
    return int(connect().execute("PRAGMA user_version").fetchone()[0])


def init_db() -> None:
    """Create or upgrade the database in place. Data is never dropped; a copy is taken before upgrading."""
    with _init_lock:
        conn = connect()
        old = int(conn.execute("PRAGMA user_version").fetchone()[0])
        has_data = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] > 0
        if has_data and old < SCHEMA_VERSION:
            from backend.services import backup  # local import: backup uses this module
            backup.backup_database(f"before-schema-{SCHEMA_VERSION}")
        conn.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        if old < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _decode(table: str, row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in JSON_FIELDS.get(table, ()):
        if f in d and d[f] is not None:
            try:
                d[f] = json.loads(d[f])
            except (TypeError, json.JSONDecodeError):
                pass
    return d


def _encode(table: str, values: dict) -> dict:
    out = {}
    for k, v in values.items():
        if k in JSON_FIELDS.get(table, ()) and v is not None and not isinstance(v, str):
            out[k] = json.dumps(v, ensure_ascii=False)
        else:
            out[k] = v
    return out


_ALLOWED_TABLES = set(JSON_FIELDS)


def _check_table(table: str) -> None:
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"unknown table {table}")


def _check_columns(cols) -> None:
    for c in cols:
        if not c.replace("_", "").isalnum():
            raise ValueError(f"bad column {c}")


def insert(table: str, values: dict) -> dict:
    _check_table(table)
    vals = _encode(table, values)
    _check_columns(vals)
    cols = ", ".join(vals)
    qs = ", ".join("?" for _ in vals)
    connect().execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(vals.values()))
    return get(table, values["id"])


def update(table: str, row_id: str, values: dict) -> None:
    _check_table(table)
    if not values:
        return
    vals = _encode(table, values)
    _check_columns(vals)
    sets = ", ".join(f"{k} = ?" for k in vals)
    connect().execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*vals.values(), row_id])


def get(table: str, row_id: str) -> dict | None:
    _check_table(table)
    row = connect().execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return _decode(table, row)


def query(table: str, where: str = "", params: tuple | list = (), order: str = "") -> list[dict]:
    """Select rows. `where`/`order` must be developer-written SQL fragments (never user input)."""
    _check_table(table)
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    if order:
        sql += f" ORDER BY {order}"
    return [_decode(table, r) for r in connect().execute(sql, params).fetchall()]


def delete(table: str, row_id: str) -> None:
    _check_table(table)
    connect().execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))


def scalar(sql: str, params: tuple = ()) -> Any:
    row = connect().execute(sql, params).fetchone()
    return row[0] if row else None
