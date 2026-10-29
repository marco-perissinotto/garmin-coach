"""Recompute the derived layer (``metrics_daily``) from the normalized tables. Offline and deterministic."""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..athlete import Athlete
from . import load as load_m
from . import recovery

DEFAULT_REST_HR = 50


def activity_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM activity WHERE start_local IS NOT NULL ORDER BY start_local"
    )]


def hr_zones(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    r = conn.execute(
        "SELECT body FROM profile_snapshot WHERE kind='hr_zones' ORDER BY taken_at DESC LIMIT 1"
    ).fetchone()
    body = json.loads(r["body"]) if r else None
    return body if isinstance(body, list) else []


def zone_for(zones: list[dict[str, Any]], sport: str = "DEFAULT") -> dict[str, Any]:
    return next((z for z in zones if z.get("sport") == sport), next(iter(zones), {}))


@dataclass
class MetricsResult:
    points: list[load_m.LoadPoint] = field(default_factory=list)
    calibration: float = 1.0
    trimp_days: int = 0  # days whose load uses the TRIMP fallback
    activity_load: dict[int, float] = field(default_factory=dict)

    def at(self, d: date) -> load_m.LoadPoint | None:
        for p in reversed(self.points):
            if p.day == d:
                return p
            if p.day < d:
                return None
        return None


def compute_loads(acts: list[dict[str, Any]], athlete: Athlete, zones: list[dict[str, Any]],
                  rest_by_day: dict[str, int]) -> tuple[dict[int, float], float, set[str]]:
    """Per-activity load: Garmin's activityTrainingLoad, else calibrated TRIMP. Returns (load per id, factor,
    days using the fallback)."""
    z = zone_for(zones)
    max_hr = athlete.max_hr or z.get("maxHeartRateUsed")
    rest_default = z.get("restingHeartRateUsed") or DEFAULT_REST_HR

    def t(a: dict[str, Any]) -> float | None:
        rest = rest_by_day.get((a["start_local"] or "")[:10], rest_default)
        return load_m.trimp(a.get("duration_s"), a.get("avg_hr"), rest, max_hr, athlete.sex)

    factor = load_m.calibration_factor((a["training_load"], t(a) or 0) for a in acts if a.get("training_load"))
    loads: dict[int, float] = {}
    fallback_days: set[str] = set()
    for a in acts:
        if a.get("training_load") is not None:
            loads[a["activity_id"]] = float(a["training_load"])
            continue
        tr = t(a)
        if tr is not None:
            loads[a["activity_id"]] = tr * factor
            fallback_days.add(a["start_local"][:10])
    return loads, factor, fallback_days


def recompute(conn: sqlite3.Connection, athlete: Athlete, today: date) -> MetricsResult:
    acts = activity_rows(conn)
    if not acts:
        conn.execute("DELETE FROM metrics_daily")
        return MetricsResult()
    zones = hr_zones(conn)
    daily = {r["date"]: dict(r) for r in conn.execute("SELECT * FROM daily")}
    rest_by_day = {d: r["rhr"] for d, r in daily.items() if r.get("rhr")}
    loads, factor, fallback_days = compute_loads(acts, athlete, zones, rest_by_day)
    per_day: dict[date, float] = {}
    for a in acts:
        d = date.fromisoformat(a["start_local"][:10])
        per_day[d] = per_day.get(d, 0.0) + loads.get(a["activity_id"], 0.0)
    start = date.fromisoformat(acts[0]["start_local"][:10])
    points = load_m.compute(load_m.daily_series(per_day, start, max(today, start)))

    sleep = {r["date"]: r["score"] for r in conn.execute("SELECT date, score FROM sleep")}
    hrv_rows = [dict(r) for r in conn.execute("SELECT * FROM hrv ORDER BY date")]
    hrv_idx = 0
    window: list[dict[str, Any]] = []
    rows = []
    prev_ctl = None
    for p in points:
        ds = p.day.isoformat()
        while hrv_idx < len(hrv_rows) and hrv_rows[hrv_idx]["date"] <= ds:
            window.append(hrv_rows[hrv_idx])
            window = window[-7:]
            hrv_idx += 1
        hv = recovery.hrv_view(window) if window and window[-1]["date"] == ds else None
        day_row = daily.get(ds, {})
        rhr_avg = day_row.get("rhr_7d_avg")
        if rhr_avg is None:
            prev = [v for i in range(1, 8)
                    if (v := daily.get((p.day - timedelta(days=i)).isoformat(), {}).get("rhr"))]
            rhr_avg = statistics.fmean(prev) if len(prev) >= 4 else None
        rd = recovery.readiness(hv, sleep.get(ds), day_row.get("bb_at_wake"),
                                recovery.rhr_delta(day_row.get("rhr"), rhr_avg), p.tsb, prev_ctl)
        prev_ctl = p.ctl
        rows.append((ds, round(p.load, 1), round(p.atl, 2), round(p.ctl, 2),
                     None if p.tsb is None else round(p.tsb, 2),
                     None if p.acwr is None else round(p.acwr, 3),
                     None if p.monotony is None else round(p.monotony, 3),
                     None if p.strain is None else round(p.strain, 1),
                     rd.score, json.dumps(rd.reasons, ensure_ascii=False) if rd.score is not None else None))
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM metrics_daily")
        conn.executemany(
            "INSERT INTO metrics_daily(date, load, atl, ctl, tsb, acwr, monotony, strain, readiness_score, "
            "readiness_reasons) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return MetricsResult(points, factor, len(fallback_days), loads)
