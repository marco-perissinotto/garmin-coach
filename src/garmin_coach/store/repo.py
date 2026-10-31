"""Upsert/read helpers for the raw, sync and tier layers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from ..clock import iso, parse_iso
from ..secrets import redact


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def save_raw(conn: sqlite3.Connection, endpoint: str, key: str, body: Any, now: datetime) -> tuple[int, bool]:
    """Store a response verbatim. Returns (raw_id, is_new_content). Identical body only bumps last_seen_at."""
    text = dumps(body)
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ts = iso(now)
    row = conn.execute(
        "SELECT id FROM raw_payload WHERE endpoint=? AND key=? AND content_hash=?", (endpoint, key, h)
    ).fetchone()
    if row:
        conn.execute("UPDATE raw_payload SET last_seen_at=? WHERE id=?", (ts, row["id"]))
        return int(row["id"]), False
    cur = conn.execute(
        "INSERT INTO raw_payload(endpoint, key, fetched_at, last_seen_at, content_hash, body) VALUES (?,?,?,?,?,?)",
        (endpoint, key, ts, ts, h, text),
    )
    return int(cur.lastrowid or 0), True


def latest_raw(conn: sqlite3.Connection, endpoint: str, key: str) -> Any | None:
    row = conn.execute(
        "SELECT body FROM raw_payload WHERE endpoint=? AND key=? ORDER BY last_seen_at DESC, id DESC LIMIT 1",
        (endpoint, key),
    ).fetchone()
    return json.loads(row["body"]) if row else None


def set_sync_state(
    conn: sqlite3.Connection, endpoint: str, key: str, status: int | None, now: datetime, note: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO sync_state(endpoint, key, last_fetched_at, last_status, note) VALUES (?,?,?,?,?) "
        "ON CONFLICT(endpoint, key) DO UPDATE SET last_fetched_at=excluded.last_fetched_at, "
        "last_status=excluded.last_status, note=COALESCE(excluded.note, sync_state.note)",
        (endpoint, key, iso(now), status, note),
    )


def get_sync_state(conn: sqlite3.Connection, endpoint: str, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sync_state WHERE endpoint=? AND key=?", (endpoint, key)).fetchone()


REJECT_DAYS = 7  # an endpoint that 400s even on a small window is skipped this long, then probed again


def learned_cap(conn: sqlite3.Connection, endpoint: str) -> int | None:
    """Learned window cap; 0 means the endpoint is currently disabled (rejected)."""
    row = conn.execute("SELECT cap_days FROM endpoint_cap WHERE endpoint=?", (endpoint,)).fetchone()
    return int(row["cap_days"]) if row else None


def endpoint_rejected(conn: sqlite3.Connection, endpoint: str, now: datetime) -> bool:
    row = conn.execute("SELECT cap_days, learned_at FROM endpoint_cap WHERE endpoint=?", (endpoint,)).fetchone()
    if not row or row["cap_days"] != 0:
        return False
    since = parse_iso(row["learned_at"])
    if since is None or now - since > timedelta(days=REJECT_DAYS):
        conn.execute("DELETE FROM endpoint_cap WHERE endpoint=?", (endpoint,))  # probe again
        return False
    return True


def set_endpoint_rejected(conn: sqlite3.Connection, endpoint: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO endpoint_cap(endpoint, cap_days, learned_at) VALUES (?,0,?) "
        "ON CONFLICT(endpoint) DO UPDATE SET cap_days=0, learned_at=excluded.learned_at",
        (endpoint, iso(now)),
    )


def set_learned_cap(conn: sqlite3.Connection, endpoint: str, cap_days: int, now: datetime) -> None:
    conn.execute(
        "INSERT INTO endpoint_cap(endpoint, cap_days, learned_at) VALUES (?,?,?) "
        "ON CONFLICT(endpoint) DO UPDATE SET cap_days=MIN(endpoint_cap.cap_days, excluded.cap_days), "
        "learned_at=excluded.learned_at",
        (endpoint, cap_days, iso(now)),
    )


# -- tier_state ---------------------------------------------------------------------------------------------------
def tier_states(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT * FROM tier_state"):
        out[r["tier"]] = {
            "last_completed_at": parse_iso(r["last_completed_at"]),
            "last_attempt_at": parse_iso(r["last_attempt_at"]),
            "last_error": r["last_error"],
            "cooldown_until": parse_iso(r["cooldown_until"]),
        }
    return out


def _ensure_tier(conn: sqlite3.Connection, tier: str) -> None:
    conn.execute("INSERT OR IGNORE INTO tier_state(tier) VALUES (?)", (tier,))


def mark_tier_attempt(conn: sqlite3.Connection, tiers: Iterable[str], now: datetime) -> None:
    for t in tiers:
        _ensure_tier(conn, t)
        conn.execute("UPDATE tier_state SET last_attempt_at=? WHERE tier=?", (iso(now), t))


def mark_tier_complete(conn: sqlite3.Connection, tiers: Iterable[str], now: datetime) -> None:
    for t in tiers:
        _ensure_tier(conn, t)
        conn.execute(
            "UPDATE tier_state SET last_completed_at=?, last_error=NULL WHERE tier=?", (iso(now), t)
        )


def mark_tier_error(conn: sqlite3.Connection, tiers: Iterable[str], error: str, now: datetime) -> None:
    for t in tiers:
        _ensure_tier(conn, t)
        conn.execute(
            "UPDATE tier_state SET last_error=?, last_attempt_at=? WHERE tier=?", (redact(error)[:500], iso(now), t)
        )


def set_cooldown(conn: sqlite3.Connection, tiers: Iterable[str], until: datetime, error: str) -> None:
    for t in tiers:
        _ensure_tier(conn, t)
        conn.execute(
            "UPDATE tier_state SET cooldown_until=?, last_error=? WHERE tier=?", (iso(until), redact(error)[:500], t)
        )


def is_cache_empty(conn: sqlite3.Connection) -> bool:
    """True until a history backfill (tier H) has completed once."""
    row = conn.execute("SELECT last_completed_at FROM tier_state WHERE tier='H'").fetchone()
    return not (row and row["last_completed_at"])


# -- app_flag: tiny process-wide signals (D-081) -------------------------------------------------------------
# Set by whoever waits for the refresh lock to serve a consumer; fit jobs yield while it is fresh. A flag older than
# SYNC_WANTED_MAX_AGE_S is ignored, so a crashed process cannot stall the fit jobs.
SYNC_WANTED = "sync_wanted_at"
SYNC_WANTED_MAX_AGE_S = 30


def set_flag(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_flag(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def get_flag(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_flag WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def clear_flag(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM app_flag WHERE key=?", (key,))


def flag_age_seconds(conn: sqlite3.Connection, key: str, now: datetime) -> float | None:
    """Seconds since a timestamp flag was set; None when absent or unreadable."""
    try:
        ts = parse_iso(get_flag(conn, key))
    except ValueError:
        return None
    return None if ts is None else (now - ts).total_seconds()


def sync_wanted(conn: sqlite3.Connection, now: datetime) -> bool:
    age = flag_age_seconds(conn, SYNC_WANTED, now)
    return age is not None and age < SYNC_WANTED_MAX_AGE_S
