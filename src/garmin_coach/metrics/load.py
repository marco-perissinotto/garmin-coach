"""Training load: daily load, ATL/CTL/TSB, ACWR, monotony/strain (PLAN §7). Pure functions, see docs/METRICS.md."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

ATL_DAYS = 7
CTL_DAYS = 42
ACWR_HIGH = 1.5
ACWR_LOW = 0.8


def ewma_factor(tau_days: float) -> float:
    return 1 - math.exp(-1 / tau_days)


def ewma(values: Iterable[float], tau_days: float, start: float = 0.0) -> list[float]:
    """Exponentially weighted moving average with time constant ``tau_days`` (one value per day)."""
    k = ewma_factor(tau_days)
    out, x = [], start
    for v in values:
        x += (v - x) * k
        out.append(x)
    return out


def trimp(duration_s: float | None, avg_hr: float | None, rest_hr: float | None, max_hr: float | None,
          sex: str | None = "M") -> float | None:
    """Banister TRIMP from duration and average-HR reserve fraction. None when an input is missing/implausible."""
    if not duration_s or not avg_hr or not rest_hr or not max_hr or max_hr <= rest_hr:
        return None
    hrr = (avg_hr - rest_hr) / (max_hr - rest_hr)
    if hrr <= 0:
        return 0.0
    hrr = min(hrr, 1.0)
    a, b = (0.86, 1.67) if (sex or "M").upper().startswith("F") else (0.64, 1.92)
    return duration_s / 60 * hrr * a * math.exp(b * hrr)


def calibration_factor(pairs: Iterable[tuple[float, float]], min_samples: int = 10) -> float:
    """Median of Garmin load / TRIMP over activities that have both, so the fallback speaks Garmin's units."""
    ratios = [load / t for load, t in pairs if load and t and t > 0]
    if len(ratios) < min_samples:
        return 1.0
    return max(0.3, min(3.0, statistics.median(ratios)))


@dataclass
class LoadPoint:
    day: date
    load: float
    atl: float
    ctl: float
    tsb: float | None  # CTL - ATL of the previous day (form going into this day)
    acwr: float | None
    monotony: float | None
    strain: float | None


def daily_series(loads: dict[date, float], start: date, end: date) -> list[tuple[date, float]]:
    """One value per calendar day; a day without activities is a real zero (rest), not missing."""
    out, d = [], start
    while d <= end:
        out.append((d, loads.get(d, 0.0)))
        d += timedelta(days=1)
    return out


def compute(series: Sequence[tuple[date, float]]) -> list[LoadPoint]:
    values = [v for _, v in series]
    atl = ewma(values, ATL_DAYS)
    ctl = ewma(values, CTL_DAYS)
    points: list[LoadPoint] = []
    for i, (d, v) in enumerate(series):
        tsb = ctl[i - 1] - atl[i - 1] if i > 0 else None
        acwr = None
        if i >= 27:
            acute = sum(values[i - 6 : i + 1])
            chronic = sum(values[i - 27 : i + 1]) / 4
            acwr = acute / chronic if chronic > 0 else None
        monotony = strain = None
        if i >= 6:
            week = values[i - 6 : i + 1]
            sd = statistics.pstdev(week)
            if sd > 0:
                monotony = statistics.fmean(week) / sd
                strain = sum(week) * monotony
        points.append(LoadPoint(d, v, atl[i], ctl[i], tsb, acwr, monotony, strain))
    return points


def acwr_flag(acwr: float | None) -> str | None:
    if acwr is None:
        return None
    if acwr > ACWR_HIGH:
        return "alto"
    if acwr < ACWR_LOW:
        return "basso"
    return None
