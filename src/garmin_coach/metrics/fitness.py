"""Fitness indicators that the Personal API data supports (PLAN §7, adapted).

VO2max is not exposed by this API tier (``vO2MaxValue`` is empty on every activity), so fitness trends use
efficiency proxies instead: running speed per heartbeat on steady runs and cycling power per heartbeat on steady
rides, indoor and outdoor kept apart.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .sport import BIKE, RUN, family, is_indoor

POWER_DURATIONS = (5, 60, 300, 1200, 3600)
MIN_STEADY_S = 30 * 60


@dataclass
class Efficiency:
    label: str
    unit: str
    recent: float | None  # mean over the last 28 days
    previous: float | None  # mean over days 29-90
    n_recent: int
    n_previous: int

    @property
    def change_pct(self) -> float | None:
        if self.recent is None or not self.previous:
            return None
        return (self.recent / self.previous - 1) * 100


def _steady(a: dict[str, Any], lthr: int | None) -> bool:
    """Long enough and mostly aerobic: average HR below 90% of threshold HR (or below 150 bpm if unknown)."""
    hr = a.get("avg_hr")
    limit = lthr * 0.9 if lthr else 150
    return hr is not None and hr > 0 and (a.get("duration_s") or 0) >= MIN_STEADY_S and hr <= limit


def efficiency(activities: Sequence[dict[str, Any]], today: date, lthr_run: int | None,
               lthr_bike: int | None) -> list[Efficiency]:
    recent_from = (today - timedelta(days=28)).isoformat()
    prev_from = (today - timedelta(days=90)).isoformat()
    buckets: dict[tuple[str, bool], tuple[list[float], list[float]]] = {}
    for a in activities:
        day = (a.get("start_local") or "")[:10]
        if day < prev_from:
            continue
        fam, indoor = family(a.get("type_key")), is_indoor(a.get("type_key"))
        value = None
        if fam == RUN and _steady(a, lthr_run):
            speed = a.get("avg_gap_speed_mps") or a.get("avg_speed_mps")
            value = speed * 60 / a["avg_hr"] if speed else None  # metres per heartbeat
        elif fam == BIKE and _steady(a, lthr_bike):
            watts = a.get("norm_power") or a.get("avg_power")
            value = watts / a["avg_hr"] if watts else None  # watts per bpm
        if value is None:
            continue
        rec, prev = buckets.setdefault((fam, indoor), ([], []))
        (rec if day >= recent_from else prev).append(value)
    out = []
    labels = {
        (RUN, False): ("corsa all'aperto, metri per battito", "m/batt"),
        (RUN, True): ("corsa su tapis roulant, metri per battito", "m/batt"),
        (BIKE, False): ("bici all'aperto, watt per bpm", "W/bpm"),
        (BIKE, True): ("bici indoor, watt per bpm", "W/bpm"),
    }
    for key, (label, unit) in labels.items():
        rec, prev = buckets.get(key, ([], []))
        if not rec and not prev:
            continue
        out.append(Efficiency(label, unit, statistics.fmean(rec) if rec else None,
                              statistics.fmean(prev) if prev else None, len(rec), len(prev)))
    return out


def power_bests(activities: Sequence[dict[str, Any]], today: date, days: int = 90) -> dict[str, dict[int, int]]:
    """Best mean-maximal power per duration over ``days``, outdoor and indoor separately."""
    since = (today - timedelta(days=days)).isoformat()
    out: dict[str, dict[int, int]] = {"outdoor": {}, "indoor": {}}
    for a in activities:
        if family(a.get("type_key")) != BIKE or (a.get("start_local") or "")[:10] < since:
            continue
        where = "indoor" if is_indoor(a.get("type_key")) else "outdoor"
        for d in POWER_DURATIONS:
            v = a.get(f"pwr_{d}s")
            if v and v > out[where].get(d, 0):
                out[where][d] = v
    return {k: v for k, v in out.items() if v}


def zone_distribution(activities: Sequence[dict[str, Any]]) -> dict[int, float] | None:
    """Share of time in each Garmin HR zone (1-5) over the given activities."""
    tot = {i: sum(a.get(f"hr_z{i}_s") or 0 for a in activities) for i in range(1, 6)}
    s = sum(tot.values())
    return None if s <= 0 else {i: v / s for i, v in tot.items()}


def latest_activity_lthr(activities: Sequence[dict[str, Any]]) -> tuple[int, str] | None:
    """Garmin's per-activity lactate-threshold HR, most recent first: (bpm, date)."""
    for a in sorted(activities, key=lambda x: x.get("start_local") or "", reverse=True):
        if a.get("lthr"):
            return int(a["lthr"]), (a.get("start_local") or "")[:10]
    return None
