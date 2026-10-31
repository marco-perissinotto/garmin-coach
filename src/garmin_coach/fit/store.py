"""The DB and file side of FIT ingestion (docs/PLAN-FIT.md §4): ``activity_fit`` rows, ``activity.has_fit`` and the
kept ZIPs under ``<app data>/fit/``. Used by the runner, the CLI, the status and the MCP tools.

The runner calls :func:`prepare` (write the file, parse; no DB writes) before its ``BEGIN IMMEDIATE`` and :func:`save`
inside it, so the SQLite write lock is never held while a file is decoded (D-079). :func:`ingest` does both.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..clock import iso
from ..secrets import redact
from ..settings import Paths, Settings
from .parse import PARSER_VERSION, FitResult, parse_archive

__all__ = ["PARSER_VERSION", "coverage", "ingest", "mark", "pending_ids", "prepare", "reparse", "save", "summary"]

log = logging.getLogger(__name__)

DOWNLOAD_ENDPOINT = "activity.download"
# sync_state statuses that count as "tried": success, no file (404) and other client errors. 429 and 5xx are retried.
_ATTEMPTED = "(last_status IS NOT NULL AND last_status < 500 AND last_status <> 429)"
_ZONE_SPORT = {"corsa": "RUNNING", "bici": "CYCLING", "nuoto": "SWIMMING"}  # metrics.sport family -> Garmin zones


@dataclass
class Prepared:
    activity_id: int
    result: FitResult
    file_name: str | None
    zip_sha256: str
    zip_bytes: int


# -- eligibility ----------------------------------------------------------------------------------------------------
def skipped_type(settings: Settings, type_key: str | None) -> bool:
    return (type_key or "") in set(settings.fit.skip_types)


def attempted(conn: sqlite3.Connection, activity_id: int) -> bool:
    return conn.execute(f"SELECT 1 FROM sync_state WHERE endpoint=? AND key=? AND {_ATTEMPTED}",
                        (DOWNLOAD_ENDPOINT, str(activity_id))).fetchone() is not None


def pending_ids(conn: sqlite3.Connection, settings: Settings, today: date) -> list[int]:
    """Activities in the cache without a download attempt, newest first: type not skipped, inside
    ``fit.backfill_days`` when > 0. A 429/5xx is not an attempt (retried)."""
    days = settings.fit.backfill_days
    since = (today - timedelta(days=days)).isoformat() if days > 0 else ""
    skip = set(settings.fit.skip_types)
    rows = conn.execute(
        "SELECT a.activity_id, a.type_key FROM activity a WHERE substr(COALESCE(a.start_local,''),1,10) >= ? "
        f"AND NOT EXISTS (SELECT 1 FROM sync_state s WHERE s.endpoint=? AND s.key=CAST(a.activity_id AS TEXT) "
        f"AND {_ATTEMPTED}) ORDER BY a.start_local DESC, a.activity_id DESC",
        (since, DOWNLOAD_ENDPOINT),
    )
    return [int(r["activity_id"]) for r in rows if (r["type_key"] or "") not in skip]


def zone_floors(conn: sqlite3.Connection, activity_id: int) -> list[int] | None:
    """Garmin HR zone floors (zones 1-5) for the activity's sport, from the latest ``hr_zones`` snapshot; used only
    by the parser's computed-zones fallback."""
    from ..metrics import compute, sport  # lazy: the metrics package is not needed to read summaries

    zones = compute.hr_zones(conn)
    if not zones:
        return None
    row = conn.execute("SELECT type_key FROM activity WHERE activity_id=?", (activity_id,)).fetchone()
    wanted = _ZONE_SPORT.get(sport.family(row["type_key"] if row else None))
    z = next((x for x in zones if wanted and x.get("sport") == wanted), None) or compute.zone_for(zones)
    floors = [f for f in (z.get(f"zone{i}Floor") for i in range(1, 6)) if isinstance(f, int | float)]
    return [int(f) for f in floors] if len(floors) == 5 else None


# -- writing --------------------------------------------------------------------------------------------------------
def _write_file(paths: Paths, activity_id: int, data: bytes) -> str:
    """Atomic write (temp file + os.replace): an orphan file after a crash is harmless and overwritten next time."""
    paths.fit_dir.mkdir(parents=True, exist_ok=True)
    name = f"{activity_id}.zip"
    fd, tmp = tempfile.mkstemp(prefix=f".{activity_id}.", suffix=".tmp", dir=paths.fit_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, paths.fit_dir / name)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return name


def prepare(paths: Paths | None, settings: Settings, activity_id: int, zip_bytes: bytes, *,
            hr_zones: list[int] | None = None) -> Prepared:
    """Keep the file (when ``fit.keep_files`` and ``paths``) and parse it. No database writes."""
    file_name = None
    if settings.fit.keep_files and paths is not None:
        try:
            file_name = _write_file(paths, activity_id, zip_bytes)
        except OSError as exc:  # a full disk must not lose the parsed summary
            log.warning("FIT file for activity %d not kept: %s", activity_id, type(exc).__name__)
    result = parse_archive(zip_bytes, hr_zone_floors=hr_zones, max_laps=settings.fit.max_laps,
                           series_points=settings.fit.series_points)
    return Prepared(activity_id, result, file_name, hashlib.sha256(zip_bytes).hexdigest(), len(zip_bytes))


def save(conn: sqlite3.Connection, p: Prepared, now: datetime, *, fetched_at: str | None = None) -> str:
    """Upsert the row and ``activity.has_fit`` (inside the caller's transaction). Returns the status."""
    r = p.result
    ok = r.status == "ok"
    conn.execute(
        "INSERT INTO activity_fit(activity_id, status, fetched_at, parsed_at, parser_version, file_name, zip_sha256, "
        "zip_bytes, fit_bytes, sport, sub_sport, device, n_sessions, n_laps, n_records, data, error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(activity_id) DO UPDATE SET status=excluded.status, "
        "fetched_at=excluded.fetched_at, parsed_at=excluded.parsed_at, parser_version=excluded.parser_version, "
        "file_name=excluded.file_name, zip_sha256=excluded.zip_sha256, zip_bytes=excluded.zip_bytes, "
        "fit_bytes=excluded.fit_bytes, sport=excluded.sport, sub_sport=excluded.sub_sport, device=excluded.device, "
        "n_sessions=excluded.n_sessions, n_laps=excluded.n_laps, n_records=excluded.n_records, data=excluded.data, "
        "error=excluded.error",
        (p.activity_id, r.status, fetched_at or iso(now), iso(now), PARSER_VERSION, p.file_name, p.zip_sha256,
         p.zip_bytes, r.fit_bytes or None, r.sport, r.sub_sport, r.device, r.n_sessions if ok else None,
         r.n_laps if ok else None, r.n_records if ok else None,
         json.dumps(r.summary, separators=(",", ":"), ensure_ascii=False) if ok else None,
         redact(r.error)[:500] if r.error else None),
    )
    conn.execute("UPDATE activity SET has_fit=? WHERE activity_id=?", (int(ok), p.activity_id))
    return r.status


def ingest(conn: sqlite3.Connection, paths: Paths | None, settings: Settings, activity_id: int, zip_bytes: bytes,
           now: datetime, *, hr_zones: list[int] | None = None) -> str:
    """write file (if keep_files) -> extract -> parse -> upsert activity_fit + activity.has_fit; returns the status."""
    floors = hr_zones if hr_zones is not None else zone_floors(conn, activity_id)
    return save(conn, prepare(paths, settings, activity_id, zip_bytes, hr_zones=floors), now)


def mark(conn: sqlite3.Connection, activity_id: int, status: str, now: datetime, error: str | None = None) -> None:
    """A download with no file to parse (``not_found``: HTTP 404 or an empty body)."""
    conn.execute(
        "INSERT INTO activity_fit(activity_id, status, fetched_at, error) VALUES (?,?,?,?) "
        "ON CONFLICT(activity_id) DO UPDATE SET status=excluded.status, fetched_at=excluded.fetched_at, "
        "parsed_at=NULL, parser_version=NULL, file_name=NULL, zip_sha256=NULL, zip_bytes=NULL, fit_bytes=NULL, "
        "sport=NULL, sub_sport=NULL, device=NULL, n_sessions=NULL, n_laps=NULL, n_records=NULL, data=NULL, "
        "error=excluded.error",
        (activity_id, status, iso(now), redact(error)[:500] if error else None),
    )
    conn.execute("UPDATE activity SET has_fit=0 WHERE activity_id=?", (activity_id,))


def reparse(conn: sqlite3.Connection, paths: Paths, settings: Settings, activity_id: int, now: datetime, *,
            hr_zones: list[int] | None = None) -> str:
    """Re-derive the summary from the kept ZIP, offline. Returns the new status, or ``"no_file"`` when there is no
    row or no kept file (nothing changes then)."""
    row = conn.execute("SELECT fetched_at, file_name FROM activity_fit WHERE activity_id=?", (activity_id,)).fetchone()
    if row is None or not row["file_name"]:
        return "no_file"
    path = paths.fit_dir / row["file_name"]
    if not path.is_file():
        return "no_file"
    data = path.read_bytes()
    floors = hr_zones if hr_zones is not None else zone_floors(conn, activity_id)
    result = parse_archive(data, hr_zone_floors=floors, max_laps=settings.fit.max_laps,
                           series_points=settings.fit.series_points)
    p = Prepared(activity_id, result, row["file_name"], hashlib.sha256(data).hexdigest(), len(data))
    conn.execute("BEGIN IMMEDIATE")
    try:
        status = save(conn, p, now, fetched_at=row["fetched_at"])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return status


def reparse_candidates(conn: sqlite3.Connection, *, everything: bool = False) -> tuple[list[int], list[int]]:
    """(rows with a kept file to re-parse, rows that would need one but have none). Default: older parser version
    or ``parse_error``; ``everything``: every row with a parse attempt."""
    cond = "1=1" if everything else f"(status='parse_error' OR parser_version IS NULL OR parser_version < {PARSER_VERSION})"
    rows = conn.execute(f"SELECT activity_id, file_name FROM activity_fit WHERE status<>'not_found' AND {cond} "
                        "ORDER BY activity_id DESC").fetchall()
    return ([int(r["activity_id"]) for r in rows if r["file_name"]],
            [int(r["activity_id"]) for r in rows if not r["file_name"]])


# -- reading --------------------------------------------------------------------------------------------------------
def summary(conn: sqlite3.Connection, activity_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT data FROM activity_fit WHERE activity_id=? AND status='ok'", (activity_id,)).fetchone()
    return json.loads(row["data"]) if row and row["data"] else None


def activity_status(conn: sqlite3.Connection, settings: Settings, activity_id: int, type_key: str | None) -> str:
    """``fit_status`` of the MCP answer: ok | no_file | parse_error | not_downloaded | disabled | skipped_type."""
    row = conn.execute("SELECT status FROM activity_fit WHERE activity_id=?", (activity_id,)).fetchone()
    if row is not None:
        return {"ok": "ok", "parse_error": "parse_error"}.get(row["status"], "no_file")
    if not settings.fit.download:
        return "disabled"
    if skipped_type(settings, type_key):
        return "skipped_type"
    return "not_downloaded"


def files_mb(paths: Paths) -> float:
    total = 0
    try:
        with os.scandir(paths.fit_dir) as it:
            total = sum(e.stat().st_size for e in it if e.is_file() and e.name.endswith(".zip"))
    except FileNotFoundError:
        pass
    return round(total / 1_048_576, 1)


def coverage(conn: sqlite3.Connection, settings: Settings, paths: Paths, today: date) -> dict[str, Any]:
    """Counts for ``status`` / ``get_sync_status`` (PLAN-FIT §8.3); the job fields are added by status.py."""
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM activity_fit GROUP BY status")}
    return {
        "enabled": settings.fit.download,
        "ok": counts.get("ok", 0),
        "parse_error": counts.get("parse_error", 0),
        "no_file": counts.get("no_fit", 0) + counts.get("not_found", 0),
        "pending": len(pending_ids(conn, settings, today)),
        "files_mb": files_mb(paths),
    }
