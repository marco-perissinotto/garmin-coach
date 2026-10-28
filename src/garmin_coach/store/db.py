"""SQLite connection (WAL), schema creation and migrations."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1

# Index = target version; MIGRATIONS[n] upgrades from version n to n+1. v0 -> v1 is the base schema.
MIGRATIONS: list[str] = []


def _schema_sql() -> str:
    return resources.files("garmin_coach.store").joinpath("schema.sql").read_text(encoding="utf-8")


def connect(path: Path | str, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Open the cache DB, creating/migrating the schema. Use ':memory:' in tests."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(_schema_sql())
        version = 1
    while version < SCHEMA_VERSION:
        conn.executescript(MIGRATIONS[version - 1])
        version += 1
    conn.execute(f"PRAGMA user_version = {version}")
