"""DateWeightDTO (weight/all and weight/range) -> weight table, one row per weigh-in."""

from __future__ import annotations

import sqlite3
from typing import Any

from ._util import as_list, day, to_float, ts, upsert_coalesce


def _grams(v: Any) -> float | None:
    """The spec documents kilograms; Connect historically returns grams. Values < 1000 are taken as kg."""
    f = to_float(v)
    if f is None:
        return None
    return f * 1000 if f < 1000 else f


def _row(w: dict[str, Any], source: str) -> dict[str, Any]:
    stamp = ts(w.get("timestampGMT")) or ts(w.get("date"))
    return {
        "ts_gmt": stamp,
        "date": day(w.get("calendarDate")) or day(w.get("date")),
        "weight_g": _grams(w.get("weight")),
        "bmi": to_float(w.get("bmi")),
        "body_fat_pct": to_float(w.get("bodyFat")),
        "muscle_mass_g": _grams(w.get("muscleMass")),
        "body_water_pct": to_float(w.get("bodyWater")),
        "source": source,
    }


def apply_all(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    for w in as_list(body, "dateWeightList"):
        if isinstance(w, dict):
            upsert_coalesce(conn, "weight", "ts_gmt", _row(w, "weight.all"))


def apply_range(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    if not isinstance(body, dict):
        return
    for summary in body.get("dailyWeightSummaries") or []:
        if not isinstance(summary, dict):
            continue
        entries = summary.get("allWeightMetrics") or ([summary["latestWeight"]] if summary.get("latestWeight") else [])
        for w in entries:
            if isinstance(w, dict):
                upsert_coalesce(conn, "weight", "ts_gmt", _row(w, "weight.range"))
