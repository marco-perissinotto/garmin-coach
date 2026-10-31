"""raw_payload -> normalized tables. Pure, offline, deterministic (PLAN §5.3).

Payloads are applied in ``last_seen_at, id`` order, so the newest observation wins. Incremental normalization
after a sync and a full ``rebuild_all`` replay produce the same tables.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from typing import Any

from . import activities, daily, hrv, profile, sleep, weight

log = logging.getLogger(__name__)

Handler = Callable[[sqlite3.Connection, str, Any, str], None]

HANDLERS: dict[str, Handler] = {
    "activities.search": activities.apply_search,
    "activity.detail": activities.apply_detail,
    "usersummary.daily": daily.apply_summary_v1,
    "usersummary.range": daily.apply_summary_v2,
    "hr.daily": daily.apply_heart_rate,
    "bodybattery.range": daily.apply_body_battery,
    "stress.range": daily.apply_stress,
    "steps.range": daily.apply_steps,
    "hydration.range": daily.apply_hydration,
    "sleep.daily": sleep.apply,
    "sleep.range": sleep.apply,
    "hrv.daily": hrv.apply_daily,
    "hrv.range": hrv.apply_range,
    "weight.all": weight.apply_all,
    "weight.range": weight.apply_range,
}

NORMALIZED_TABLES = ("activity", "daily", "sleep", "hrv", "weight", "profile_snapshot")


def _apply_rows(conn: sqlite3.Connection, rows: Iterable[sqlite3.Row]) -> int:
    n = 0
    for r in rows:
        endpoint = r["endpoint"]
        try:
            body = json.loads(r["body"])
            if endpoint in profile.KINDS:
                profile.apply(conn, endpoint, body, r["fetched_at"])
            elif endpoint in HANDLERS:
                HANDLERS[endpoint](conn, r["key"], body, r["last_seen_at"])
            else:
                continue  # raw-only endpoints (workouts, spo2, wellness activities, opt-ins)
            n += 1
        except Exception:  # noqa: BLE001 - unknown shapes must never crash a sync (PLAN §5.3)
            log.exception("normalize failed for raw_payload id=%s endpoint=%s", r["id"], endpoint)
    return n


def apply_ids(conn: sqlite3.Connection, raw_ids: Iterable[int]) -> int:
    """Normalize the given raw rows inside the caller's transaction."""
    ids = sorted(set(raw_ids))
    if not ids:
        return 0
    rows = conn.execute(
        "SELECT * FROM raw_payload WHERE id IN (SELECT value FROM json_each(?)) ORDER BY last_seen_at, id",
        (json.dumps(ids),),
    )
    return _apply_rows(conn, rows)


def apply(conn: sqlite3.Connection, raw_ids: Iterable[int]) -> int:
    conn.execute("BEGIN")
    try:
        n = apply_ids(conn, raw_ids)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return n


def rebuild_all(conn: sqlite3.Connection) -> int:
    """Drop the normalized layer and rebuild it from raw_payload, without network access."""
    conn.execute("BEGIN")
    try:
        for t in NORMALIZED_TABLES:
            conn.execute(f"DELETE FROM {t}")
        n = _apply_rows(conn, conn.execute("SELECT * FROM raw_payload ORDER BY last_seen_at, id"))
        # FIT summaries are not part of the normalized layer (no re-parse here, D-074): restore the flag.
        conn.execute("UPDATE activity SET has_fit=1 WHERE activity_id IN "
                     "(SELECT activity_id FROM activity_fit WHERE status='ok')")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return n
