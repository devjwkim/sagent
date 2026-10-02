"""SQLite access.

One connection per call (WAL + busy_timeout), schema kept as a single string of
CREATE TABLE IF NOT EXISTS statements. `_sync_columns` adds columns declared in
SCHEMA but missing from an existing database, so additive changes need no
hand-written migration.
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    is_active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    session_epoch INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, created_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    status INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    is_secret INTEGER NOT NULL DEFAULT 0,
    updated_by INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    lifecycle TEXT NOT NULL DEFAULT 'BOOTSTRAP',
    primary_agent TEXT NOT NULL DEFAULT 'claude',
    created_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    added_by INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_members_user ON project_members(user_id);

CREATE TABLE IF NOT EXISTS project_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    result_json TEXT NOT NULL,
    created_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scans_project ON project_scans(project_id, id);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

_db_path: Path | None = None
_extra_schemas: list[str] = []

# Feature modules that call register_schema() at import time.
SCHEMA_MODULES = ("sagent.core.runs", "sagent.core.loops", "sagent.core.tests", "sagent.core.reviews", "sagent.core.usage", "sagent.core.prompts",
                  "sagent.core.telemetry", "sagent.core.loop_library", "sagent.core.tokens")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def register_schema(sql: str) -> None:
    """Let feature modules contribute their own CREATE statements."""
    if sql not in _extra_schemas:
        _extra_schemas.append(sql)


def full_schema() -> str:
    return SCHEMA + "\n".join(_extra_schemas)


def configure(path: Path | str) -> None:
    global _db_path
    _db_path = Path(path)


def db_path() -> Path:
    if _db_path is None:
        raise RuntimeError("database not configured; call db.configure() first")
    return _db_path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def query_one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: tuple | dict = ()) -> int:
    """Run a write statement; returns lastrowid."""
    with connect() as conn:
        return conn.execute(sql, params).lastrowid


def scalar(sql: str, params: tuple | dict = ()) -> Any:
    row = query_one(sql, params)
    return row[0] if row else None


_CREATE_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", re.S | re.I
)


def _declared_columns(schema: str) -> dict[str, list[tuple[str, str]]]:
    tables: dict[str, list[tuple[str, str]]] = {}
    for name, body in _CREATE_RE.findall(schema):
        cols = []
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line or line.split()[0].upper() in {
                "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"
            }:
                continue
            col, _, rest = line.partition(" ")
            cols.append((col, rest))
        tables[name] = cols
    return tables


def _sync_columns(conn: sqlite3.Connection, schema: str) -> list[str]:
    added = []
    for table, cols in _declared_columns(schema).items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols:
            if col in existing:
                continue
            upper = decl.upper()
            if "PRIMARY KEY" in upper or "UNIQUE" in upper:
                continue
            if "NOT NULL" in upper and "DEFAULT" not in upper:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            added.append(f"{table}.{col}")
    return added


def init_db(path: Path | str | None = None) -> None:
    import importlib

    for mod in SCHEMA_MODULES:
        importlib.import_module(mod)
    if path is not None:
        configure(path)
    target = db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    new = not target.exists()
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        schema = full_schema()
        conn.executescript(schema)
        _sync_columns(conn, schema)
        conn.commit()
    finally:
        conn.close()
    if new:
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
