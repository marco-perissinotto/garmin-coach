"""Tolerant field access and coalescing upserts shared by the normalizers (PLAN §5.3)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any


def dig(obj: Any, *path: str) -> Any:
    """obj['a']['b'] that returns None on any missing level or wrong type."""
    cur = obj
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def to_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def to_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_str(v: Any) -> str | None:
    return None if v is None else str(v)


def ms_div(v: Any, d: float = 1000.0) -> int | None:
    f = to_float(v)
    return None if f is None else int(round(f / d))


def ts(v: Any) -> str | None:
    """Garmin timestamps come as epoch ms or ISO-ish strings. Returns 'YYYY-MM-DDTHH:MM:SS' (no zone)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, dict):  # ConnectTimestamp {"date": ..., "localDate": ...}
        return ts(v.get("date"))
    if isinstance(v, int | float):
        return datetime.fromtimestamp(v / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    s = str(v).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1]
    return s[:19] if len(s) >= 19 else s


def day(v: Any) -> str | None:
    """Calendar date 'YYYY-MM-DD' from a string, epoch ms or ConnectDate object."""
    if v is None:
        return None
    if isinstance(v, dict):
        return day(v.get("localDate") or v.get("date"))
    t = ts(v)
    return t[:10] if t and len(t) >= 10 else None


def upsert_coalesce(conn: sqlite3.Connection, table: str, pk: str, row: dict[str, Any]) -> None:
    """Insert or update; a NULL in the new row never erases a known value (newest non-null wins)."""
    if row.get(pk) is None:
        return
    cols = list(row)
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=COALESCE(excluded.{c}, {table}.{c})" for c in cols if c != pk)
    conn.execute(
        f"INSERT INTO {table}({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT({pk}) DO UPDATE SET {updates}",
        [row[c] for c in cols],
    )


def as_list(body: Any, *keys: str) -> list[Any]:
    if isinstance(body, list):
        return body
    for k in keys:
        v = dig(body, k)
        if isinstance(v, list):
            return v
    return []
