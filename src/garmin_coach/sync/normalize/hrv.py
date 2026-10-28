"""HrvSummaryDTO (from HrvChartDTO or HrvDailySummaryDTO) -> hrv table. Readings stay in raw only."""

from __future__ import annotations

import sqlite3
from typing import Any

from ._util import day, dig, to_float, to_int, to_str, upsert_coalesce


def _row(s: dict[str, Any], seen_at: str) -> dict[str, Any]:
    return {
        "date": day(s.get("calendarDate")),
        "last_night_avg": to_int(s.get("lastNightAvg")),
        "last_night_5min_high": to_int(s.get("lastNight5MinHigh")),
        "weekly_avg": to_int(s.get("weeklyAvg")),
        "baseline_low_upper": to_int(dig(s, "baseline", "lowUpper")),
        "baseline_balanced_low": to_int(dig(s, "baseline", "balancedLow")),
        "baseline_balanced_upper": to_int(dig(s, "baseline", "balancedUpper")),
        "baseline_marker": to_float(dig(s, "baseline", "markerValue")),
        "status": to_str(s.get("status")),
        "feedback": to_str(s.get("feedbackPhrase")),
        "updated_at": seen_at,
    }


def apply_daily(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    s = dig(body, "hrvSummary")
    if isinstance(s, dict):
        row = _row(s, seen_at)
        row["date"] = row["date"] or key
        upsert_coalesce(conn, "hrv", "date", row)


def apply_range(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    for s in dig(body, "hrvSummaries") or []:
        if isinstance(s, dict):
            upsert_coalesce(conn, "hrv", "date", _row(s, seen_at))
