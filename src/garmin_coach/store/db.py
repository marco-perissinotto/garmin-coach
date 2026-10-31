"""SQLite connection (WAL), schema creation and migrations."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 4

# MIGRATIONS[n - 1] upgrades from version n to n + 1 (v0 -> v1 is the base schema).
# The flag says whether the normalized layer must be rebuilt from raw_payload afterwards (offline, no network).
MIGRATIONS: list[tuple[str, bool]] = [
    (  # v1 -> v2 (Phase 2): per-activity fields needed by the metrics
        """
        ALTER TABLE activity ADD COLUMN parent INTEGER;
        ALTER TABLE activity ADD COLUMN avg_run_cadence REAL;
        ALTER TABLE activity ADD COLUMN avg_gap_speed_mps REAL;
        ALTER TABLE activity ADD COLUMN hr_z1_s REAL;
        ALTER TABLE activity ADD COLUMN hr_z2_s REAL;
        ALTER TABLE activity ADD COLUMN hr_z3_s REAL;
        ALTER TABLE activity ADD COLUMN hr_z4_s REAL;
        ALTER TABLE activity ADD COLUMN hr_z5_s REAL;
        ALTER TABLE activity ADD COLUMN pwr_5s INTEGER;
        ALTER TABLE activity ADD COLUMN pwr_60s INTEGER;
        ALTER TABLE activity ADD COLUMN pwr_300s INTEGER;
        ALTER TABLE activity ADD COLUMN pwr_1200s INTEGER;
        ALTER TABLE activity ADD COLUMN pwr_3600s INTEGER;
        """,
        True,
    ),
    (  # v2 -> v3 (Phase 2): fingerprint of the data the derived layer was computed from
        "CREATE TABLE IF NOT EXISTS derived_state (key TEXT PRIMARY KEY, value TEXT);",
        False,
    ),
    (  # v3 -> v4 (Phase 5a, docs/PLAN-FIT.md §4.1): FIT summaries + process-wide flags; existing rows untouched
        """
        CREATE TABLE IF NOT EXISTS activity_fit (
          activity_id INTEGER PRIMARY KEY, status TEXT NOT NULL, fetched_at TEXT NOT NULL, parsed_at TEXT,
          parser_version INTEGER, file_name TEXT, zip_sha256 TEXT, zip_bytes INTEGER, fit_bytes INTEGER,
          sport TEXT, sub_sport TEXT, device TEXT, n_sessions INTEGER, n_laps INTEGER, n_records INTEGER,
          data TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS app_flag (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """,
        False,
    ),
]


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
    rebuild = False
    if version == 0:
        conn.executescript(_schema_sql())
        version = 1
    while version < SCHEMA_VERSION:
        sql, needs_rebuild = MIGRATIONS[version - 1]
        conn.executescript(sql)
        rebuild = rebuild or needs_rebuild
        version += 1
        conn.execute(f"PRAGMA user_version = {version}")
    conn.execute(f"PRAGMA user_version = {version}")
    if rebuild and conn.execute("SELECT 1 FROM raw_payload LIMIT 1").fetchone():
        from ..sync import normalize  # lazy: store must not import sync at module load

        normalize.rebuild_all(conn)
