"""Recovery: HRV vs baseline, RHR delta, sleep, Body Battery and the heuristic readiness score (PLAN §7).

Readiness is a transparent rule-based heuristic, not Garmin's Training Readiness (not exposed by the Personal API).
Reasons are written in Italian because they go straight into the packs (advice_language = it).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

RHR_DELTA_FLAG = 5
WEIGHTS = {"hrv": 30, "sleep": 25, "bb": 20, "rhr": 15, "tsb": 10}
HRV_STATUS_IT = {"BALANCED": "bilanciato", "UNBALANCED": "sbilanciato", "LOW": "basso", "POOR": "scarso",
                 "NONE": "non disponibile", "INVALID": "non valido"}


def hrv_status_it(status: str | None) -> str:
    return HRV_STATUS_IT.get(status or "", (status or "–").lower())


@dataclass
class HrvView:
    last_night: int | None
    weekly_avg: int | None
    status: str | None
    balanced_low: int | None
    balanced_high: int | None
    nights_below: int  # consecutive most recent nights below the balanced range
    slope_7d: float | None  # ms per day, least squares over the last 7 nights


def slope(values: Sequence[float | None]) -> float | None:
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 3:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return None if den == 0 else sum((x - mx) * (y - my) for x, y in pts) / den


def hrv_view(rows: Sequence[dict]) -> HrvView | None:
    """``rows``: hrv rows oldest -> newest, the last one is last night."""
    if not rows:
        return None
    last = rows[-1]
    below = 0
    for r in reversed(rows):
        lo, v = r.get("baseline_balanced_low"), r.get("last_night_avg")
        if lo is None or v is None or v >= lo:
            break
        below += 1
    return HrvView(
        last_night=last.get("last_night_avg"),
        weekly_avg=last.get("weekly_avg"),
        status=last.get("status"),
        balanced_low=last.get("baseline_balanced_low"),
        balanced_high=last.get("baseline_balanced_upper"),
        nights_below=below,
        slope_7d=slope([r.get("last_night_avg") for r in rows[-7:]]),
    )


def rhr_delta(rhr: int | None, rhr_7d_avg: float | None) -> int | None:
    if rhr is None or rhr_7d_avg is None:
        return None
    return round(rhr - rhr_7d_avg)


@dataclass
class Readiness:
    score: int | None
    reasons: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _hrv_points(v: HrvView | None) -> tuple[float | None, str | None]:
    if v is None or (v.status in (None, "NONE", "INVALID") and v.last_night is None):
        return None, None
    base = {"BALANCED": 1.0, "UNBALANCED": 0.6, "LOW": 0.35, "POOR": 0.15}.get(v.status or "", 0.6)
    # Garmin's status follows the 7-day average, so it can stay BALANCED while recent nights fall below the
    # range: penalise by consecutive nights below (1 night -0.25, 3+ nights -0.5).
    if v.nights_below >= 3:
        base -= 0.5
    elif v.nights_below >= 1:
        base -= 0.25
    base = max(base, 0.0)
    rng = f" (intervallo {v.balanced_low}–{v.balanced_high})" if v.balanced_low and v.balanced_high else ""
    return base, f"HRV {v.last_night} ms, stato {hrv_status_it(v.status)}{rng}"


def _sleep_points(score: int | None) -> tuple[float | None, str | None]:
    if score is None:
        return None, None
    return max(min(score / 100, 1.0), 0.0), f"sonno punteggio {score}"


def _bb_points(bb: int | None) -> tuple[float | None, str | None]:
    if bb is None:
        return None, None
    return max(min(bb / 100, 1.0), 0.0), f"Body Battery al risveglio {bb}"


def _rhr_points(delta: int | None) -> tuple[float | None, str | None]:
    if delta is None:
        return None, None
    pts = 1.0 if delta <= 0 else 0.8 if delta <= 2 else 0.5 if delta < RHR_DELTA_FLAG else 0.2
    return pts, f"FC a riposo {delta:+d} bpm sulla media 7 giorni"


def _tsb_points(tsb: float | None, ctl: float | None) -> tuple[float | None, str | None]:
    if tsb is None or not ctl:
        return None, None
    form = tsb / ctl  # relative form: comparable whatever the absolute load scale
    pts = 1.0 if form >= -0.10 else 0.6 if form >= -0.30 else 0.3 if form >= -0.50 else 0.0
    return pts, f"forma (TSB) {form:+.0%} del carico cronico"


def readiness(hrv: HrvView | None, sleep_score: int | None, bb_at_wake: int | None, rhr_d: int | None,
              tsb: float | None, ctl: float | None) -> Readiness:
    """0-100: HRV 30, sleep 25, Body Battery 20, RHR delta 15, TSB 10. Missing parts are dropped and the remaining
    weights rescaled; with less than half of the weight available the score is None."""
    parts = {
        "hrv": _hrv_points(hrv),
        "sleep": _sleep_points(sleep_score),
        "bb": _bb_points(bb_at_wake),
        "rhr": _rhr_points(rhr_d),
        "tsb": _tsb_points(tsb, ctl),
    }
    names = {"hrv": "HRV", "sleep": "sonno", "bb": "Body Battery", "rhr": "FC a riposo", "tsb": "forma"}
    r = Readiness(score=None)
    got = 0.0
    weight = 0
    for k, (pts, why) in parts.items():
        if pts is None:
            r.missing.append(names[k])
            continue
        got += pts * WEIGHTS[k]
        weight += WEIGHTS[k]
        if why:
            r.reasons.append(why)
    if weight >= 50:
        r.score = round(got / weight * 100)
    return r


def readiness_label(score: int | None) -> str:
    if score is None:
        return "non calcolabile"
    if score >= 80:
        return "alta"
    if score >= 60:
        return "buona"
    if score >= 40:
        return "ridotta"
    return "bassa"
