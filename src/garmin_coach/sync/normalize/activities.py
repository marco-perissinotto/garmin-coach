"""SummarizedActivityDTO rows (activity search) -> activity table. Detail/workouts stay raw (PLAN §5.3)."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from ...store.repo import dumps
from ._util import dig, to_float, to_int, to_str, ts

COLUMNS = (
    "activity_id", "start_local", "start_gmt", "type_key", "name", "distance_m", "duration_s", "moving_s",
    "elev_gain_m", "avg_hr", "max_hr", "avg_power", "norm_power", "aerobic_te", "anaerobic_te", "te_label",
    "training_load", "vo2max", "lthr", "ftp", "avg_speed_mps", "calories", "body_battery_diff", "summary_hash",
    "updated_at",
)


def summary_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(dumps(row).encode("utf-8")).hexdigest()


def from_summary(a: dict[str, Any], seen_at: str) -> dict[str, Any]:
    return {
        "activity_id": to_int(a.get("activityId")),
        "start_local": ts(a.get("startTimeLocal")),
        "start_gmt": ts(a.get("startTimeGMT")),
        "type_key": to_str(dig(a, "activityType", "typeKey")),
        "name": to_str(a.get("activityName")),
        "distance_m": to_float(a.get("distance")),
        "duration_s": to_float(a.get("duration")),
        "moving_s": to_float(a.get("movingDuration")),
        "elev_gain_m": to_float(a.get("elevationGain")),
        "avg_hr": to_int(a.get("averageHR")),
        "max_hr": to_int(a.get("maxHR")),
        "avg_power": to_float(a.get("avgPower")),
        "norm_power": to_float(a.get("normPower")),
        "aerobic_te": to_float(a.get("aerobicTrainingEffect")),
        "anaerobic_te": to_float(a.get("anaerobicTrainingEffect")),
        "te_label": to_str(a.get("trainingEffectLabel")),
        "training_load": to_float(a.get("activityTrainingLoad")),
        "vo2max": to_float(a.get("vO2MaxValue")),
        "lthr": to_int(a.get("lactateThresholdBpm")),
        "ftp": to_int(a.get("maxFtp")),
        "avg_speed_mps": to_float(a.get("averageSpeed")),
        "calories": to_float(a.get("calories")),
        "body_battery_diff": to_int(a.get("differenceBodyBattery")),
        "summary_hash": summary_hash(a),
        "updated_at": seen_at,
    }


def apply_search(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    if not isinstance(body, list):
        return
    for a in body:
        if not isinstance(a, dict):
            continue
        row = from_summary(a, seen_at)
        if row["activity_id"] is None:
            continue
        cols = list(row)
        # Full replace of the summary columns (list rows are complete); has_detail/has_fit are never touched here.
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "activity_id")
        conn.execute(
            f"INSERT INTO activity({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) "
            f"ON CONFLICT(activity_id) DO UPDATE SET {updates}",
            [row[c] for c in cols],
        )


def apply_detail(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    activity_id = to_int(key)
    if activity_id is None:
        return
    conn.execute(
        "INSERT INTO activity(activity_id, has_detail) VALUES (?, 1) ON CONFLICT(activity_id) DO UPDATE SET has_detail=1",
        (activity_id,),
    )
