"""DailySleepDTO (single day or range) -> sleep table."""

from __future__ import annotations

import sqlite3
from typing import Any

from ._util import as_list, day, dig, to_float, to_int, to_str, ts, upsert_coalesce


def _row(s: dict[str, Any], seen_at: str) -> dict[str, Any]:
    return {
        "date": day(s.get("calendarDate")),
        "start_local": ts(s.get("sleepStartTimestampLocal")),
        "end_local": ts(s.get("sleepEndTimestampLocal")),
        "total_s": to_int(s.get("sleepTimeSeconds")),
        "deep_s": to_int(s.get("deepSleepSeconds")),
        "light_s": to_int(s.get("lightSleepSeconds")),
        "rem_s": to_int(s.get("remSleepSeconds")),
        "awake_s": to_int(s.get("awakeSleepSeconds")),
        "awake_count": to_int(s.get("awakeCount")),
        "score": to_int(dig(s, "sleepScores", "overall", "value")),
        "score_quality": to_str(dig(s, "sleepScores", "overall", "qualifierKey")),
        "avg_hr": to_int(s.get("avgHeartRate")),
        "avg_spo2": to_float(s.get("averageSpO2Value")),
        "lowest_spo2": to_float(s.get("lowestSpO2Value")),
        "avg_respiration": to_float(s.get("averageRespirationValue")),
        "avg_stress": to_float(s.get("avgSleepStress")),
        "nap_s": to_int(s.get("napTimeSeconds")),
        "sleep_need_min": to_int(dig(s, "sleepNeed", "actual")),
        "feedback": to_str(s.get("sleepScoreFeedback")),
        "updated_at": seen_at,
    }


def apply(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    items: list[Any]
    if isinstance(body, dict):
        # The spec returns DailySleepDTO directly; Connect web wraps it in dailySleepDTO. Accept both.
        items = [body.get("dailySleepDTO", body)]
    else:
        items = as_list(body)
    for s in items:
        if isinstance(s, dict):
            upsert_coalesce(conn, "sleep", "date", _row(s, seen_at))
