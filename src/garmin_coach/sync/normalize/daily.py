"""Daily wellness (summary v1/v2, HR, Body Battery, stress, steps, hydration) -> daily table."""

from __future__ import annotations

import sqlite3
from typing import Any

from ._util import as_list, day, dig, ms_div, to_float, to_int, upsert_coalesce


def _put(conn: sqlite3.Connection, row: dict[str, Any], seen_at: str) -> None:
    if row.get("date"):
        row["updated_at"] = seen_at
        upsert_coalesce(conn, "daily", "date", row)


def apply_summary_v1(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    """UserDailySummaryDto from /usersummary-service/usersummary/daily."""
    if not isinstance(body, dict):
        return
    _put(conn, {
        "date": day(body.get("calendarDate")) or key,
        "steps": to_int(body.get("totalSteps")),
        "distance_m": to_float(body.get("totalDistanceMeters")),
        "active_kcal": to_float(body.get("activeKilocalories")),
        "total_kcal": to_float(body.get("totalKilocalories")),
        "rhr": to_int(body.get("restingHeartRate")),
        "rhr_7d_avg": to_int(body.get("lastSevenDaysAvgRestingHeartRate")),
        "hr_min": to_int(body.get("minHeartRate")),
        "hr_max": to_int(body.get("maxHeartRate")),
        "stress_avg": to_int(body.get("averageStressLevel")),
        "stress_high_s": to_int(body.get("highStressDuration")),
        "stress_med_s": to_int(body.get("mediumStressDuration")),
        "stress_low_s": to_int(body.get("lowStressDuration")),
        "stress_rest_s": to_int(body.get("restStressDuration")),
        "bb_high": to_int(body.get("bodyBatteryHighestValue")),
        "bb_low": to_int(body.get("bodyBatteryLowestValue")),
        "bb_at_wake": to_int(body.get("bodyBatteryAtWakeTime")),
        "bb_charged": to_int(body.get("bodyBatteryChargedValue")),
        "bb_drained": to_int(body.get("bodyBatteryDrainedValue")),
        "intensity_min_moderate": to_int(body.get("moderateIntensityMinutes")),
        "intensity_min_vigorous": to_int(body.get("vigorousIntensityMinutes")),
        "spo2_avg": to_float(body.get("averageSpo2")),
        "respiration_avg": to_float(body.get("avgWakingRespirationValue")),
        "hydration_ml": to_float(body.get("hydrationValueInML")),
    }, seen_at)


def apply_summary_v2(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    """ApiDocumentListUserDailySummaryV2DTO {data: [UserDailySummaryV2DTO]}."""
    for d in as_list(body, "data"):
        if not isinstance(d, dict):
            continue
        _put(conn, {
            "date": day(d.get("calendarDate")),
            "steps": to_int(dig(d, "movement", "steps", "value")),
            "distance_m": to_float(dig(d, "movement", "steps", "distanceInMeters")),
            "active_kcal": to_float(dig(d, "calories", "burnedActive")),
            "total_kcal": to_float(dig(d, "calories", "burnedTotal")),
            "rhr": to_int(dig(d, "heartRate", "restingValue")),
            "hr_min": to_int(dig(d, "heartRate", "minValue")),
            "hr_max": to_int(dig(d, "heartRate", "maxValue")),
            "stress_avg": to_int(dig(d, "stress", "avgLevel")),
            "stress_high_s": ms_div(dig(d, "stress", "highStressDurationInMillis")),
            "stress_med_s": ms_div(dig(d, "stress", "mediumStressDurationInMillis")),
            "stress_low_s": ms_div(dig(d, "stress", "lowStressDurationInMillis")),
            "stress_rest_s": ms_div(dig(d, "stress", "restDurationInMillis")),
            "bb_high": to_int(dig(d, "bodyBattery", "maxValue")),
            "bb_low": to_int(dig(d, "bodyBattery", "minValue")),
            "bb_charged": to_int(dig(d, "bodyBattery", "chargedValue")),
            "bb_drained": to_int(dig(d, "bodyBattery", "drainedValue")),
            "intensity_min_moderate": to_int(dig(d, "intensityMinutes", "moderate")),
            "intensity_min_vigorous": to_int(dig(d, "intensityMinutes", "vigorous")),
            "spo2_avg": to_float(dig(d, "pulseOx", "avgValue")),
            "respiration_avg": to_float(dig(d, "respiration", "avgValue")),
            "hydration_ml": to_float(dig(d, "hydration", "consumedInFractionalMl")),
        }, seen_at)


def apply_heart_rate(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    """DailyHeartRateDTO. Intraday samples stay in raw only."""
    if not isinstance(body, dict):
        return
    _put(conn, {
        "date": day(body.get("calendarDate")) or key,
        "rhr": to_int(body.get("restingHeartRate")),
        "rhr_7d_avg": to_int(body.get("lastSevenDaysAvgRestingHeartRate")),
        "hr_min": to_int(body.get("minHeartRate")),
        "hr_max": to_int(body.get("maxHeartRate")),
    }, seen_at)


def apply_body_battery(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    """[BodyBatteryDailyReportDTO]. The curve stays in raw only."""
    for r in as_list(body):
        if isinstance(r, dict):
            _put(conn, {
                "date": day(r.get("date")),
                "bb_charged": to_int(r.get("charged")),
                "bb_drained": to_int(r.get("drained")),
            }, seen_at)


def apply_stress(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    """[MultiValueWellnessChartItemDTO]; durations in seconds."""
    for r in as_list(body):
        if isinstance(r, dict):
            v = r.get("values") or {}
            _put(conn, {
                "date": day(r.get("calendarDate")),
                "stress_avg": to_int(dig(v, "overallStressLevel")),
                "stress_high_s": to_int(dig(v, "highStressDuration")),
                "stress_med_s": to_int(dig(v, "mediumStressDuration")),
                "stress_low_s": to_int(dig(v, "lowStressDuration")),
                "stress_rest_s": to_int(dig(v, "restStressDuration")),
            }, seen_at)


def apply_steps(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    for r in as_list(body):
        if isinstance(r, dict):
            _put(conn, {
                "date": day(r.get("calendarDate")),
                "steps": to_int(r.get("totalSteps")),
                "distance_m": to_float(r.get("totalDistance")),
            }, seen_at)


def apply_hydration(conn: sqlite3.Connection, key: str, body: Any, seen_at: str) -> None:
    for r in as_list(body):
        if isinstance(r, dict):
            _put(conn, {"date": day(r.get("calendarDate")), "hydration_ml": to_float(r.get("valueInML"))}, seen_at)
