"""The dashboard's numbers: every series, comparison and trend the page shows, computed from the cache with the same
metrics the packs use (docs/DASHBOARD.md). No network, no LLM. The sentences for the athlete are in ``insights.py``.

Keys of the payload are English (`run`, `bike`, `swim`, `other`); the page and the insights carry the Italian labels.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..athlete import Event, Inputs
from ..context import fmt
from ..metrics import compute, fitness, sport
from ..metrics import events as events_m
from ..metrics import load as load_m

HISTORY_DAYS = 1096  # daily series: three years
WEEKS = 156  # weekly series: three years
RECENT_DAYS = 42  # "now" window of a sport trend
PREVIOUS_DAYS = 90  # the window before it
FLAT_PCT = 1.5  # a trend within +-1.5 % is "stable"
MIN_TREND_N = 3
LOW_FRACTION = 0.89  # of the threshold HR: the top of the aerobic domain (Friel zone 2)
RACE_EFFORT = 0.80  # of max HR: a single-sport activity at the event's distance counts as a past edition above this
POWER_DURATIONS = fitness.POWER_DURATIONS
RUN_BESTS = ((5.0, "5 km"), (10.0, "10 km"), (21.0975, "mezza"), (42.195, "maratona"))
SPORT_KEYS = {sport.RUN: "run", sport.BIKE: "bike", sport.SWIM: "swim"}
KEYS = ("run", "bike", "swim", "other")
EVENT_FAMILY = {"running": sport.RUN, "run": sport.RUN, "corsa": sport.RUN, "trail": sport.RUN,
                "cycling": sport.BIKE, "bike": sport.BIKE, "bici": sport.BIKE,
                "swimming": sport.SWIM, "swim": sport.SWIM, "nuoto": sport.SWIM,
                "triathlon": sport.MULTI, "duathlon": sport.MULTI, "multisport": sport.MULTI}
RUN_DYNAMICS = (("cadence_spm", "cadenza", "passi/min", 0), ("stance_time_ms", "tempo di contatto", "ms", 0),
                ("vertical_ratio_pct", "rapporto verticale", "%", 1), ("step_length_m", "lunghezza del passo", "m", 2),
                ("stance_time_balance_pct", "contatto a sinistra", "%", 1))
# key, label, unit, direction that is good (+1 up, -1 down, 0 neither), decimals, measured at night (today counts)
HEALTH = (("hrv", "HRV notturna", "ms", 1, 0, True), ("rhr", "FC a riposo", "bpm", -1, 0, True),
          ("sleep_h", "Sonno", "h", 1, 1, True), ("sleep_score", "Punteggio del sonno", "", 1, 0, True),
          ("deep_rem", "Sonno profondo + REM", "%", 1, 0, True), ("resp", "Respirazione notturna", "atti/min", -1, 1, True),
          ("spo2", "SpO2 notturna", "%", 1, 1, True), ("stress", "Stress medio", "", -1, 0, False),
          ("bb_high", "Body Battery massima", "", 1, 0, False), ("steps", "Passi", "", 0, 0, False))
HEALTH_Z = 0.75  # a 7-day mean this many daily standard deviations away from the 90-day mean is out of the usual
FIT_SQL = """SELECT f.activity_id, json_extract(f.data, '$.zones.hr') AS zhr, json_extract(f.data, '$.dynamics') AS dyn,
  json_extract(f.data, '$.records.decoupling_pct') AS dec, json_extract(f.data, '$.session.avg_swolf') AS swolf,
  json_extract(f.data, '$.session.pool_length_m') AS pool
  FROM activity_fit f JOIN activity a USING(activity_id) WHERE f.status = 'ok' AND a.start_local >= ?"""


# -- small statistics -----------------------------------------------------------------------------------------------
def _vals(xs: Iterable[float | None]) -> list[float]:
    return [x for x in xs if x is not None]


def mean(xs: Iterable[float | None]) -> float | None:
    v = _vals(xs)
    return statistics.fmean(v) if v else None


def median(xs: Iterable[float | None]) -> float | None:
    v = _vals(xs)
    return statistics.median(v) if v else None


def stdev(xs: Iterable[float | None]) -> float | None:
    v = _vals(xs)
    return statistics.pstdev(v) if len(v) >= 2 else None


def pct(new: float | None, old: float | None) -> float | None:
    return None if new is None or not old else (new / old - 1) * 100


def _r(v: float | None, nd: int = 1) -> float | None:
    return None if v is None else round(v, nd)


def key_of(family: str) -> str:
    return SPORT_KEYS.get(family, "other")


# -- context --------------------------------------------------------------------------------------------------------
@dataclass
class Ctx:
    today: date
    inputs: Inputs
    acts: list[dict[str, Any]]  # oldest first, with day, fam, key, indoor, secs, load_v
    fit: dict[int, dict[str, Any]]
    daily: dict[str, dict[str, Any]]
    sleep: dict[str, dict[str, Any]]
    hrv: dict[str, dict[str, Any]]
    metrics: dict[str, dict[str, Any]]  # the whole history
    zones: list[dict[str, Any]]
    max_hr: float | None
    lthr: dict[str, float | None]

    @property
    def start(self) -> date:
        return self.today - timedelta(days=HISTORY_DAYS - 1)

    def since(self, days: int) -> date:
        """First day of a window of ``days`` days ending today."""
        return self.today - timedelta(days=days - 1)


def _json(text: Any) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def load(conn: sqlite3.Connection, inputs: Inputs, today: date) -> Ctx:
    """Read what the page needs in a handful of queries. ``metrics_daily`` must be current (builder.ensure_metrics)."""
    since = (today - timedelta(days=HISTORY_DAYS - 1)).isoformat()
    acts = [a for a in compute.activity_rows(conn) if a["start_local"][:10] <= today.isoformat()]
    zones = compute.hr_zones(conn)
    rest = {r["date"]: r["rhr"] for r in conn.execute("SELECT date, rhr FROM daily WHERE rhr IS NOT NULL")}
    loads, _, _ = compute.compute_loads(acts, inputs.athlete, zones, rest)
    for a in acts:
        a["day"] = date.fromisoformat(a["start_local"][:10])
        a["fam"] = sport.family(a.get("type_key"))
        a["key"] = key_of(a["fam"])
        a["indoor"] = sport.is_indoor(a.get("type_key"))
        a["secs"] = a.get("moving_s") or a.get("duration_s") or 0.0
        a["load_v"] = loads.get(a["activity_id"], 0.0)
    fit = {r["activity_id"]: {"zhr": _json(r["zhr"]), "dyn": _json(r["dyn"]), "dec": r["dec"], "swolf": r["swolf"],
                              "pool": r["pool"]} for r in conn.execute(FIT_SQL, (since,))}

    def table(name: str) -> dict[str, dict[str, Any]]:
        return {r["date"]: dict(r) for r in conn.execute(f"SELECT * FROM {name} WHERE date >= ?", (since,))}

    by_sport = {z.get("sport"): z for z in zones}
    default = compute.zone_for(zones)
    athlete = inputs.athlete
    max_hr = athlete.max_hr or default.get("maxHeartRateUsed")

    def lthr(name: str) -> float | None:
        return ((by_sport.get(name) or {}).get("lactateThresholdHeartRateUsed") or athlete.lthr
                or default.get("lactateThresholdHeartRateUsed") or (0.9 * max_hr if max_hr else None))

    return Ctx(today=today, inputs=inputs, acts=acts, fit=fit, daily=table("daily"), sleep=table("sleep"),
               hrv=table("hrv"), metrics={r["date"]: dict(r) for r in conn.execute("SELECT * FROM metrics_daily")},
               zones=zones, max_hr=max_hr,
               lthr={"run": lthr("RUNNING"), "bike": lthr("CYCLING"), "swim": lthr("SWIMMING"),
                     "other": lthr("DEFAULT")})


# -- intensity domains ----------------------------------------------------------------------------------------------
def split_domains(time_s: Sequence[float | None], bounds: Sequence[float], t1: float,
                  t2: float) -> tuple[float, float, float]:
    """Time in zone -> seconds below ``t1``, between ``t1`` and ``t2``, above ``t2``.

    ``time_s[i]`` is the time between ``bounds[i - 1]`` and ``bounds[i]``: entry 0 lies below ``bounds[0]`` and the
    entry after the last bound above it (the FIT ``time_in_zone`` layout, docs/FIT.md). A bin crossed by a threshold
    is shared in proportion, as if the time were spread evenly over it.
    """
    low = mid = high = 0.0
    n = len(bounds)
    for i, secs in enumerate(time_s[: n + 1]):
        if not secs:
            continue
        lo = bounds[i - 1] if i > 0 else bounds[0] - 40
        hi = bounds[i] if i < n else bounds[-1] + 15
        span = max(hi - lo, 1.0)
        f_low = min(max(min(hi, t1) - lo, 0.0) / span, 1.0)
        f_high = min(max(hi - max(lo, t2), 0.0) / span, 1.0)
        low += secs * f_low
        high += secs * f_high
        mid += secs * max(1.0 - f_low - f_high, 0.0)
    return low, mid, high


def domains(ctx: Ctx, a: dict[str, Any]) -> tuple[float, float, float] | None:
    """Low / medium / high intensity seconds of one activity: FIT time in zone with the threshold HR the device had
    that day, else Garmin's five zone times with today's zone floors."""
    zhr = (ctx.fit.get(a["activity_id"]) or {}).get("zhr") or {}
    time_s, bounds = zhr.get("time_s"), zhr.get("boundaries_bpm") or zhr.get("floors_bpm")
    t2 = zhr.get("threshold_hr") or ctx.lthr.get(a["key"]) or ctx.lthr.get("other")
    if not (time_s and bounds):
        time_s = [0.0] + [a.get(f"hr_z{i}_s") or 0.0 for i in range(1, 6)]
        z = compute.zone_for(ctx.zones, {"run": "RUNNING", "bike": "CYCLING"}.get(a["key"], "DEFAULT"))
        bounds = [z.get(f"zone{i}Floor") for i in range(1, 6)]
        if not any(time_s) or not all(bounds):
            return None
    if not t2:
        return None
    return split_domains(time_s, [float(b) for b in bounds if b], LOW_FRACTION * t2, t2)


# -- trends ---------------------------------------------------------------------------------------------------------
Point = tuple[date, float]


def trend(points: Sequence[Point], today: date, *, higher_is_better: bool = True) -> dict[str, Any]:
    """Median of the last 42 days against the 90 days before and against the same weeks one year ago."""
    def window(first: int, last: int) -> list[float]:
        return [v for d, v in points if last <= (today - d).days < first]

    rec, prev = window(RECENT_DAYS, 0), window(RECENT_DAYS + PREVIOUS_DAYS, RECENT_DAYS)
    year = window(365 + RECENT_DAYS, 365 - 14)
    recent, previous, last_year = median(rec), median(prev), median(year)
    out: dict[str, Any] = {"recent": recent, "previous": previous, "last_year": last_year, "n_recent": len(rec),
                           "n_previous": len(prev), "n_last_year": len(year), "change_pct": None, "yoy_pct": None,
                           "better_pct": None, "verdict": None}
    sign = 1 if higher_is_better else -1
    if len(rec) >= MIN_TREND_N and len(prev) >= MIN_TREND_N:
        out["change_pct"] = pct(recent, previous)
        if out["change_pct"] is not None:
            out["better_pct"] = better = sign * out["change_pct"]
            out["verdict"] = "up" if better >= FLAT_PCT else "down" if better <= -FLAT_PCT else "flat"
    if len(rec) >= MIN_TREND_N and len(year) >= MIN_TREND_N:
        out["yoy_pct"] = pct(recent, last_year)
    return out


def rolling_median(points: Sequence[Point], days: int = RECENT_DAYS, min_n: int = MIN_TREND_N) -> list[list[Any]]:
    out = []
    for i, (d, _) in enumerate(points):
        win = [v for dd, v in points[: i + 1] if (d - dd).days < days]
        if len(win) >= min_n:
            out.append([d.isoformat(), round(statistics.median(win), 3)])
    return out


def _series(points: Sequence[Point], ctx: Ctx, nd: int = 3) -> dict[str, Any]:
    recent = [(d, v) for d, v in points if d >= ctx.start]
    return {"points": [[d.isoformat(), round(v, nd)] for d, v in recent], "line": rolling_median(recent)}


def _compare(values: Sequence[Point], today: date, nd: int = 1) -> dict[str, Any]:
    """Median over the last 42 days and over the 90 days before, with the counts (dynamics, decoupling, balance)."""
    rec = [v for d, v in values if (today - d).days < RECENT_DAYS]
    prev = [v for d, v in values if RECENT_DAYS <= (today - d).days < RECENT_DAYS + PREVIOUS_DAYS]
    r, p = median(rec), median(prev)
    return {"recent": _r(r, nd), "previous": _r(p, nd), "n_recent": len(rec), "n_previous": len(prev),
            "delta": None if r is None or p is None else round(r - p, nd)}


# -- load -----------------------------------------------------------------------------------------------------------
def sport_ctl(ctx: Ctx) -> tuple[date | None, dict[str, list[float]]]:
    """CTL per sport from the first activity to today. The four series add up to the total CTL."""
    if not ctx.acts:
        return None, {}
    first = ctx.acts[0]["day"]
    n = (ctx.today - first).days + 1
    per: dict[str, list[float]] = {k: [0.0] * n for k in KEYS}
    for a in ctx.acts:
        per[a["key"]][(a["day"] - first).days] += a["load_v"]
    return first, {k: load_m.ewma(v, load_m.CTL_DAYS) for k, v in per.items()}


def hero(ctx: Ctx) -> dict[str, Any] | None:
    m = ctx.metrics
    cur = m.get(ctx.today.isoformat())
    if not cur or cur.get("ctl") is None:
        return None

    def ctl_ago(days: int) -> float | None:
        return (m.get((ctx.today - timedelta(days=days)).isoformat()) or {}).get("ctl")

    history = [r for r in m.values() if r.get("ctl") is not None]
    peak = max(history, key=lambda r: r["ctl"])
    year_from = (ctx.today - timedelta(days=364)).isoformat()
    peak_year = max((r for r in history if r["date"] >= year_from), key=lambda r: r["ctl"])
    ctl = cur["ctl"]
    yesterday = ctl_ago(1)
    reasons = _json(cur.get("readiness_reasons")) or []
    return {
        "ctl": round(ctl, 1), "atl": round(cur["atl"], 1), "tsb": _r(cur.get("tsb")),
        "form": None if cur.get("tsb") is None or not yesterday else round(cur["tsb"] / yesterday, 3),
        "acwr": _r(cur.get("acwr"), 2), "monotony": _r(cur.get("monotony"), 2),
        "ctl_28": _r(ctl_ago(28)), "ctl_7": _r(ctl_ago(7)), "ctl_365": _r(ctl_ago(365)),
        "rank": round(sum(1 for r in history if r["ctl"] < ctl) / len(history), 3), "history_days": len(history),
        "history_from": min(r["date"] for r in history),
        "peak": {"ctl": round(peak["ctl"], 1), "date": peak["date"]},
        "peak_year": {"ctl": round(peak_year["ctl"], 1), "date": peak_year["date"]},
        "readiness": {"score": cur.get("readiness_score"), "reasons": reasons},
    }


def daily_series(ctx: Ctx, first: date | None, by_sport: dict[str, list[float]]) -> dict[str, Any]:
    days = [ctx.start + timedelta(days=i) for i in range(HISTORY_DAYS)]
    names = ("load", "ctl", "atl", "tsb", "readiness", "hrv", "hrv_lo", "hrv_hi", "rhr", "sleep_h", "need_h",
             "sleep_score", "deep_rem", "resp", "spo2", "stress", "bb_high", "steps",
             "ctl_run", "ctl_bike", "ctl_swim", "ctl_other")
    out: dict[str, list[Any]] = {k: [None] * HISTORY_DAYS for k in names}
    for i, d in enumerate(days):
        ds = d.isoformat()
        m, dd, sl, hv = ctx.metrics.get(ds), ctx.daily.get(ds), ctx.sleep.get(ds), ctx.hrv.get(ds)
        if m:
            out["load"][i], out["ctl"][i], out["atl"][i] = _r(m["load"], 0), _r(m["ctl"]), _r(m["atl"])
            out["tsb"][i], out["readiness"][i] = _r(m.get("tsb")), m.get("readiness_score")
        if dd:
            out["rhr"][i], out["stress"][i] = dd.get("rhr"), dd.get("stress_avg")
            out["bb_high"][i], out["steps"][i] = dd.get("bb_high"), dd.get("steps")
        if sl and sl.get("total_s"):
            out["sleep_h"][i] = round(sl["total_s"] / 3600, 2)
            out["need_h"][i] = None if not sl.get("sleep_need_min") else round(sl["sleep_need_min"] / 60, 2)
            out["sleep_score"][i] = sl.get("score")
            out["deep_rem"][i] = round(((sl.get("deep_s") or 0) + (sl.get("rem_s") or 0)) / sl["total_s"] * 100, 1)
            out["resp"][i], out["spo2"][i] = sl.get("avg_respiration"), sl.get("avg_spo2")
        if hv:
            out["hrv"][i] = hv.get("last_night_avg")
            out["hrv_lo"][i], out["hrv_hi"][i] = hv.get("baseline_balanced_low"), hv.get("baseline_balanced_upper")
        if first is not None and d >= first:
            for k in KEYS:
                out[f"ctl_{k}"][i] = round(by_sport[k][(d - first).days], 1)
    return {"dates": [d.isoformat() for d in days], **out}


def ctl_history(ctx: Ctx) -> list[list[Any]]:
    """Weekly CTL since the first activity: the long view."""
    rows = [r for r in sorted(ctx.metrics.values(), key=lambda r: r["date"]) if r.get("ctl") is not None]
    first = next((i for i, r in enumerate(rows) if r["ctl"] >= 1), len(rows))  # skip the empty years before
    return [[r["date"], round(r["ctl"], 1)] for r in rows[first:][::-1][::7][::-1]]


def weekly(ctx: Ctx) -> dict[str, Any]:
    monday = ctx.today - timedelta(days=ctx.today.weekday())
    start = monday - timedelta(weeks=WEEKS - 1)

    def zeros() -> list[float]:
        return [0.0] * WEEKS

    hours = {k: zeros() for k in KEYS}
    zones = {k: zeros() for k in ("low", "mid", "high")}
    load, run_km, bike_km, swim_km, long_run, long_ride = zeros(), zeros(), zeros(), zeros(), zeros(), zeros()
    sessions = [0] * WEEKS
    for a in ctx.acts:
        if a["day"] < start:
            continue
        w = (a["day"] - start).days // 7
        hours[a["key"]][w] += a["secs"] / 3600
        load[w] += a["load_v"]
        sessions[w] += 1
        km = (a.get("distance_m") or 0) / 1000
        if a["key"] == "run":
            run_km[w] += km
            long_run[w] = max(long_run[w], km)
        elif a["key"] == "bike":
            bike_km[w] += km
            long_ride[w] = max(long_ride[w], a["secs"] / 3600)
        elif a["key"] == "swim":
            swim_km[w] += km
        dom = domains(ctx, a)
        if dom:
            for k, v in zip(("low", "mid", "high"), dom, strict=True):
                zones[k][w] += v / 3600

    def rnd(xs: list[float], nd: int = 2) -> list[float]:
        return [round(x, nd) for x in xs]

    return {"weeks": [(start + timedelta(weeks=i)).isoformat() for i in range(WEEKS)],
            "hours": {k: rnd(v) for k, v in hours.items()}, "zones": {k: rnd(v) for k, v in zones.items()},
            "load": rnd(load, 0), "run_km": rnd(run_km, 1), "bike_km": rnd(bike_km, 1), "swim_km": rnd(swim_km, 2),
            "long_run": rnd(long_run, 1), "long_ride": rnd(long_ride, 2), "sessions": sessions}


def intensity(ctx: Ctx, days: int = 28) -> dict[str, Any] | None:
    """Share of low / medium / high intensity time over the last ``days`` and over the window before it."""
    def share(first: date, last: date) -> dict[str, float] | None:
        tot = [0.0, 0.0, 0.0]
        for a in ctx.acts:
            if first <= a["day"] <= last and (dom := domains(ctx, a)):
                tot = [t + v for t, v in zip(tot, dom, strict=True)]
        s = sum(tot)
        return None if s < 3600 else {"low": round(tot[0] / s, 3), "mid": round(tot[1] / s, 3),
                                      "high": round(tot[2] / s, 3), "hours": round(s / 3600, 1)}

    now = share(ctx.since(days), ctx.today)
    if now is None:
        return None
    return {"days": days, "now": now,
            "previous": share(ctx.since(days * 2), ctx.since(days) - timedelta(days=1)),
            "thresholds": {k: None if not v else [round(LOW_FRACTION * v), round(v)] for k, v in ctx.lthr.items()}}


def years(ctx: Ctx, n: int = 3) -> dict[str, Any]:
    """Cumulative hours per day of the year, this year against the previous ones, and the totals to the same date."""
    labels = [ctx.today.year - i for i in range(n - 1, -1, -1)]
    per_day: dict[int, list[float]] = {y: [0.0] * 366 for y in labels}
    to_date: dict[int, dict[str, dict[str, float]]] = {
        y: {k: {"hours": 0.0, "km": 0.0, "n": 0} for k in KEYS} for y in labels}
    cutoff = (ctx.today.month, ctx.today.day)
    for a in ctx.acts:
        y = a["day"].year
        if y not in per_day:
            continue
        per_day[y][a["day"].timetuple().tm_yday - 1] += a["secs"] / 3600
        if (a["day"].month, a["day"].day) <= cutoff:
            t = to_date[y][a["key"]]
            t["hours"] += a["secs"] / 3600
            t["km"] += (a.get("distance_m") or 0) / 1000
            t["n"] += 1
    today_idx = ctx.today.timetuple().tm_yday - 1
    cumulative: dict[str, list[float | None]] = {}
    for y in labels:
        total, series = 0.0, []
        for i, v in enumerate(per_day[y]):
            total += v
            series.append(None if y == ctx.today.year and i > today_idx else round(total, 1))
        cumulative[str(y)] = series
    return {"years": [str(y) for y in labels], "cumulative": cumulative, "day_of_year": today_idx + 1,
            "to_date": {str(y): {k: {"hours": round(v["hours"], 1), "km": round(v["km"], 1), "n": int(v["n"])}
                                 for k, v in d.items()} for y, d in to_date.items()}}


def consistency(ctx: Ctx, days: int = 28) -> dict[str, Any]:
    active = {a["day"] for a in ctx.acts if a["secs"] >= 900}
    streak, d = 0, ctx.today if ctx.today in active else ctx.today - timedelta(days=1)
    while d in active:
        streak += 1
        d -= timedelta(days=1)
    window = [ctx.today - timedelta(days=i) for i in range(days)]
    last_rest = next((x for x in (ctx.today - timedelta(days=i) for i in range(1, 400)) if x not in active), None)
    return {"days": days, "active_days": sum(1 for x in window if x in active),
            "rest_days": sum(1 for x in window if x not in active), "streak": streak,
            "last_rest": None if last_rest is None else last_rest.isoformat()}


# -- sports ---------------------------------------------------------------------------------------------------------
def _volume(ctx: Ctx, key: str) -> dict[str, Any]:
    acts = [a for a in ctx.acts if a["key"] == key]

    def tot(first: int, last: int, field: str) -> float:
        """Sum over the days ``last`` .. ``first - 1`` days ago."""
        sel = [a for a in acts if last <= (ctx.today - a["day"]).days < first]
        return sum((a.get("distance_m") or 0) / 1000 if field == "km" else a["secs"] / 3600 for a in sel)

    recent42 = [a for a in acts if (ctx.today - a["day"]).days < RECENT_DAYS]
    longest = max(recent42, key=lambda a: a["secs"], default=None)
    week_before = tot(35, 7, "km") / 4
    return {
        "km_7": round(tot(7, 0, "km"), 1), "km_week_before": round(week_before, 1),
        "ramp_pct": _r(pct(tot(7, 0, "km"), week_before), 0) if week_before >= 5 else None,
        "km_28": round(tot(28, 0, "km"), 1), "km_prev_28": round(tot(56, 28, "km"), 1),
        "hours_28": round(tot(28, 0, "h"), 1), "hours_prev_28": round(tot(56, 28, "h"), 1),
        "sessions_28": sum(1 for a in acts if (ctx.today - a["day"]).days < 28),
        "longest_42": None if longest is None else {
            "date": longest["day"].isoformat(), "hours": round(longest["secs"] / 3600, 2),
            "km": round((longest.get("distance_m") or 0) / 1000, 1)},
        "last": acts[-1]["day"].isoformat() if acts else None,
    }


def _fit_points(ctx: Ctx, acts: Iterable[dict[str, Any]], get: Callable[[dict[str, Any]], Any]) -> list[Point]:
    out = []
    for a in acts:
        v = get(ctx.fit.get(a["activity_id"]) or {})
        if isinstance(v, int | float):
            out.append((a["day"], float(v)))
    return out


def _run_dynamic(key: str) -> Callable[[dict[str, Any]], Any]:
    return lambda f: ((f.get("dyn") or {}).get("run") or {}).get(key)


def run_block(ctx: Ctx) -> dict[str, Any]:
    lthr = ctx.lthr.get("run")
    outdoor = [a for a in ctx.acts if a["key"] == "run" and not a["indoor"]]
    steady = [a for a in outdoor if fitness.is_steady(a, round(lthr) if lthr else None)
              and (a.get("avg_gap_speed_mps") or a.get("avg_speed_mps"))]

    def speed(a: dict[str, Any]) -> float:
        return float(a.get("avg_gap_speed_mps") or a["avg_speed_mps"])

    eff = [(a["day"], speed(a) * 60 / a["avg_hr"]) for a in steady]
    pace = [(a["day"], 1000 / speed(a)) for a in steady]
    hr = [(a["day"], float(a["avg_hr"])) for a in steady]
    bests = []
    for km, label in RUN_BESTS:
        cand = [(a, km * 1000 / (a["distance_m"] / a["duration_s"])) for a in outdoor
                if (a.get("distance_m") or 0) >= km * 990 and a.get("duration_s")]
        if not cand:
            continue
        by_year: dict[str, dict[str, Any]] = {}
        for a, secs in cand:
            y = str(a["day"].year)
            if y not in by_year or secs < by_year[y]["s"]:
                by_year[y] = {"s": round(secs), "date": a["day"].isoformat(), "name": a.get("name"),
                              "km": round(a["distance_m"] / 1000, 2)}
        best = min(by_year.values(), key=lambda b: b["s"])
        recent = min((secs for a, secs in cand if (ctx.today - a["day"]).days < 120), default=None)
        bests.append({"km": km, "label": label, "years": by_year, "best": best, "best_km": best["km"],
                      "recent": None if recent is None else round(recent)})
    dynamics = []
    for key, label, unit, nd in RUN_DYNAMICS:
        c = _compare(_fit_points(ctx, steady, _run_dynamic(key)), ctx.today, nd)
        if c["recent"] is not None:
            dynamics.append({"key": key, "label": label, "unit": unit, **c})
    long_runs = [a for a in outdoor if a["secs"] >= 3600]
    return {"efficiency": {**_series(eff, ctx), "trend": trend(eff, ctx.today), "unit": "m/battito"},
            "easy_pace": {**_compare(pace, ctx.today, 0), "hr": _compare(hr, ctx.today, 0)},
            "volume": _volume(ctx, "run"), "bests": bests, "dynamics": dynamics,
            "decoupling": _compare(_fit_points(ctx, long_runs, lambda f: f.get("dec")), ctx.today),
            "steady_hr_max": None if not lthr else round(lthr * 0.9)}


def bike_block(ctx: Ctx) -> dict[str, Any]:
    lthr = ctx.lthr.get("bike")
    rides = [a for a in ctx.acts if a["key"] == "bike"]
    steady = [a for a in rides if fitness.is_steady(a, round(lthr) if lthr else None)
              and (a.get("norm_power") or a.get("avg_power"))]

    def eff(indoor: bool) -> dict[str, Any]:
        pts = [(a["day"], (a.get("norm_power") or a["avg_power"]) / a["avg_hr"]) for a in steady
               if a["indoor"] == indoor]
        return {**_series(pts, ctx), "trend": trend(pts, ctx.today), "unit": "W/bpm"}

    power = []
    for d in POWER_DURATIONS:
        col = f"pwr_{d}s"
        have = [a for a in rides if a.get(col)]
        if not have:
            continue

        def best(sel: list[dict[str, Any]], col: str = col) -> dict[str, Any] | None:
            a = max(sel, key=lambda x: x[col], default=None)
            return None if a is None else {"w": int(a[col]), "date": a["day"].isoformat()}

        recent = best([a for a in have if (ctx.today - a["day"]).days < RECENT_DAYS])
        year = best([a for a in have if (ctx.today - a["day"]).days < 365])
        power.append({"seconds": d, "label": f"{d} s" if d < 60 else f"{d // 60} min", "recent": recent,
                      "year": year, "all": best(have),
                      "pct_of_year": None if not recent or not year else round(recent["w"] / year["w"] * 100)})
    twenty = max((a for a in rides if a.get("pwr_1200s") and (ctx.today - a["day"]).days < 90),
                 key=lambda a: a["pwr_1200s"], default=None)
    athlete = ctx.inputs.athlete
    outdoor = [a for a in rides if not a["indoor"]]
    return {"efficiency_outdoor": eff(False), "efficiency_indoor": eff(True), "power": power,
            "ftp": {"set": athlete.ftp, "set_date": athlete.ftp_date.isoformat() if athlete.ftp_date else None,
                    "estimate": None if twenty is None else round(twenty["pwr_1200s"] * 0.95),
                    "from_w": None if twenty is None else int(twenty["pwr_1200s"]),
                    "date": None if twenty is None else twenty["day"].isoformat()},
            "balance_right": _compare(_fit_points(
                ctx, outdoor, lambda f: ((f.get("dyn") or {}).get("bike") or {}).get("balance_right_pct")), ctx.today),
            "decoupling": _compare(_fit_points(ctx, [a for a in rides if a["secs"] >= 5400], lambda f: f.get("dec")),
                                   ctx.today),
            "volume": _volume(ctx, "bike"), "steady_hr_max": None if not lthr else round(lthr * 0.9)}


def swim_block(ctx: Ctx) -> dict[str, Any]:
    swims = [a for a in ctx.acts if a["key"] == "swim"]
    pool = [a for a in swims if a.get("type_key") == "lap_swimming" and (a.get("distance_m") or 0) >= 1000
            and a.get("avg_speed_mps")]
    pace = [(a["day"], 100 / a["avg_speed_mps"]) for a in pool]
    return {"pace": {**_series(pace, ctx, 1), "trend": trend(pace, ctx.today, higher_is_better=False),
                     "unit": "s/100 m"},
            "swolf": _compare(_fit_points(ctx, pool, lambda f: f.get("swolf")), ctx.today, 0),
            "volume": _volume(ctx, "swim")}


def strength(ctx: Ctx) -> dict[str, Any]:
    sessions = [a["day"] for a in ctx.acts if a["fam"] == sport.STRENGTH]
    return {"sessions_28": sum(1 for d in sessions if (ctx.today - d).days < 28),
            "sessions_90": sum(1 for d in sessions if (ctx.today - d).days < 90),
            "last": sessions[-1].isoformat() if sessions else None}


# -- health ---------------------------------------------------------------------------------------------------------
def _clock_minutes(ts: str | None) -> float | None:
    """Minutes from midnight of a local timestamp, evening hours negative (23:30 -> -30) so bedtimes average well."""
    if not ts or len(ts) < 16:
        return None
    m = int(ts[11:13]) * 60 + int(ts[14:16])
    return m - 1440 if m >= 720 else m


def _hhmm(minutes: float | None) -> str | None:
    if minutes is None:
        return None
    m = round(minutes) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def health(ctx: Ctx, series: dict[str, Any]) -> dict[str, Any]:
    n = len(series["dates"])
    metrics = []
    for key, label, unit, good, nd, night in HEALTH:
        col = series[key]
        end = n if night else n - 1  # day totals (stress, steps) of today are still partial
        now_v = _vals(col[end - 7 : end])
        base_v = _vals(col[max(end - 97, 0) : end - 7])
        if len(now_v) < 4 or len(base_v) < 30:
            continue
        now, base, sd = statistics.fmean(now_v), statistics.fmean(base_v), statistics.pstdev(base_v)
        z = (now - base) / sd if sd else 0.0
        status = "usual"
        if abs(z) >= HEALTH_Z:
            status = "note" if good == 0 else "good" if (z > 0) == (good > 0) else "watch"
        metrics.append({"key": key, "label": label, "unit": unit, "now": round(now, nd), "base": round(base, nd),
                        "delta": round(now - base, max(nd, 1)), "z": round(z, 2), "status": status, "good": good,
                        "decimals": nd})

    nights = [ctx.sleep[d.isoformat()] for d in (ctx.today - timedelta(days=i) for i in range(28))
              if d.isoformat() in ctx.sleep and ctx.sleep[d.isoformat()].get("total_s")]
    sleep: dict[str, Any] | None = None
    if len(nights) >= 7:
        last14 = nights[:14]
        debt = sum((s["sleep_need_min"] - s["total_s"] / 60) for s in last14 if s.get("sleep_need_min"))
        bed = [_clock_minutes(s.get("start_local")) for s in nights]
        wake = [_clock_minutes(s.get("end_local")) for s in nights]
        sleep = {"nights": len(nights), "avg_7": _r(mean(s["total_s"] / 3600 for s in nights[:7]), 2),
                 "avg_28": _r(mean(s["total_s"] / 3600 for s in nights), 2),
                 "need": _r(mean((s.get("sleep_need_min") or 0) / 60 or None for s in nights[:7]), 2),
                 "debt_14_h": round(debt / 60, 1), "bedtime": _hhmm(mean(bed)), "wake": _hhmm(mean(wake)),
                 "bedtime_sd_min": _r(stdev(bed), 0), "wake_sd_min": _r(stdev(wake), 0),
                 "awake_avg": _r(mean(s.get("awake_count") for s in nights), 1)}

    # Two days in a row with the resting HR well up and the HRV under its range: the classic early sign of illness
    # or of a load the body is not absorbing.
    alarm = None
    last2 = [ctx.today - timedelta(days=i) for i in range(2)]
    rhr_base = mean(series["rhr"][max(n - 62, 0) : n - 2])
    if rhr_base is not None:
        hits = []
        for d in last2:
            dd, hv = ctx.daily.get(d.isoformat()) or {}, ctx.hrv.get(d.isoformat()) or {}
            lo = hv.get("baseline_balanced_low")
            hits.append(bool(dd.get("rhr") and dd["rhr"] >= rhr_base + 4 and hv.get("last_night_avg")
                             and lo and hv["last_night_avg"] < lo))
        if all(hits):
            alarm = {"rhr_base": round(rhr_base), "rhr": (ctx.daily.get(ctx.today.isoformat()) or {}).get("rhr"),
                     "hrv": (ctx.hrv.get(ctx.today.isoformat()) or {}).get("last_night_avg")}
    weight = None
    w = ctx.inputs.athlete
    if w.weight_kg:
        weight = {"kg": w.weight_kg, "date": w.weight_date.isoformat() if w.weight_date else None}
    return {"metrics": metrics, "sleep": sleep, "alarm": alarm, "weight": weight}


# -- personal patterns ----------------------------------------------------------------------------------------------
def patterns(ctx: Ctx, days: int = 365, min_n: int = 8) -> dict[str, Any]:
    """Simple before/after comparisons on the athlete's own data. Associations with their counts, not causes."""
    out: dict[str, Any] = {"days": days}
    span = [ctx.today - timedelta(days=i) for i in range(days)]
    load = {d: (ctx.metrics.get(d.isoformat()) or {}).get("load") or 0.0 for d in span}
    hrv = {d: (ctx.hrv.get(d.isoformat()) or {}).get("last_night_avg") for d in span}

    # 1. the night after the hardest days against the athlete's own week before
    nonzero = sorted(v for v in load.values() if v > 0)
    if len(nonzero) >= 40:
        hard_from = nonzero[int(len(nonzero) * 0.8)]
        after: dict[str, list[float]] = {"hard_1": [], "hard_2": [], "rest_1": []}
        for d in span:
            base = mean(hrv.get(d - timedelta(days=i)) for i in range(7))
            n1, n2 = hrv.get(d + timedelta(days=1)), hrv.get(d + timedelta(days=2))
            if base is None or n1 is None:
                continue
            if load[d] >= hard_from:
                after["hard_1"].append(n1 - base)
                if n2 is not None:
                    after["hard_2"].append(n2 - base)
            elif load[d] == 0:
                after["rest_1"].append(n1 - base)
        if len(after["hard_1"]) >= min_n and len(after["rest_1"]) >= min_n:
            out["hrv_after_load"] = {"hard_from": round(hard_from), "n_hard": len(after["hard_1"]),
                                     "n_rest": len(after["rest_1"]), "hard_1": _r(mean(after["hard_1"])),
                                     "hard_2": _r(mean(after["hard_2"])), "rest_1": _r(mean(after["rest_1"]))}

    # 2. the night after a session that ended late against one that ended early
    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for a in ctx.acts:
        if a["day"] >= span[-1] and a["secs"] >= 1800:
            by_day[a["day"]].append(a)
    groups: dict[str, list[dict[str, Any]]] = {"late": [], "early": []}
    for d, acts in by_day.items():
        ends = []
        for a in acts:
            try:
                ends.append(datetime.fromisoformat(a["start_local"]) + timedelta(seconds=a.get("duration_s") or 0))
            except ValueError:
                continue
        night = ctx.sleep.get((d + timedelta(days=1)).isoformat())
        if not ends or not night or not night.get("total_s"):
            continue
        end = max(ends)
        minute = end.hour * 60 + end.minute if end.date() == d else 1440
        row = {"score": night.get("score"), "hours": night["total_s"] / 3600,
               "hrv": hrv.get(d + timedelta(days=1))}
        if minute >= 19 * 60 + 30:
            groups["late"].append(row)
        elif minute <= 17 * 60:
            groups["early"].append(row)
    if len(groups["late"]) >= min_n and len(groups["early"]) >= min_n:
        out["late_sessions"] = {
            "n_late": len(groups["late"]), "n_early": len(groups["early"]),
            **{f"{k}_{g}": _r(mean(r[k] for r in rows), 2 if k == "hours" else 1)
               for g, rows in groups.items() for k in ("score", "hours", "hrv")}}

    # 3. sleep by weekday (the day one wakes up)
    by_wd: dict[int, list[float]] = defaultdict(list)
    for d in span[:182]:
        s = ctx.sleep.get(d.isoformat())
        if s and s.get("total_s"):
            by_wd[d.weekday()].append(s["total_s"] / 3600)
    if len(by_wd) == 7 and all(len(v) >= min_n for v in by_wd.values()):
        out["sleep_by_weekday"] = [round(statistics.fmean(by_wd[i]), 2) for i in range(7)]
    return out


# -- events ---------------------------------------------------------------------------------------------------------
def events(ctx: Ctx) -> list[dict[str, Any]]:
    out = []
    for ev in events_m.event_views(ctx.inputs.events, ctx.today):
        e = ev.event
        out.append({"name": e.name, "date": e.date.isoformat(), "days": ev.days_to, "weeks": ev.weeks_to,
                    "phase": ev.phase, "focus": ev.focus, "sport": e.sport, "distance_km": e.distance_km,
                    "goal": e.goal, "priority": e.priority})
    return out


def target_event(ctx: Ctx) -> Event | None:
    """The event the season is built for: the next A-priority one, else the next B, else the next one."""
    future = [e for e in ctx.inputs.events if e.date >= ctx.today]
    for wanted in ("A", "B"):
        for e in future:
            if (e.priority or "").upper() == wanted:
                return e
    return future[0] if future else None


def event_compare(ctx: Ctx) -> dict[str, Any] | None:
    """Where the athlete stands today against the same number of days before the previous editions of the target."""
    e = target_event(ctx)
    family = EVENT_FAMILY.get((e.sport or "").lower()) if e else None
    if e is None or family is None or not e.distance_km:
        return None
    days_to = (e.date - ctx.today).days
    tol = 0.15 if family == sport.MULTI else 0.04
    editions = [a for a in ctx.acts if a["fam"] == family and (ctx.today - a["day"]).days > 120
                and abs((a.get("distance_m") or 0) / 1000 - e.distance_km) <= tol * e.distance_km
                and (family == sport.MULTI or not ctx.max_hr or (a.get("avg_hr") or 0) >= RACE_EFFORT * ctx.max_hr)]
    editions = editions[-3:]
    if not editions:
        return None
    span = min(364, days_to + 126)  # the current season shows its last 18 weeks
    secs: dict[str, dict[date, float]] = {k: defaultdict(float) for k in KEYS}
    run_km: dict[date, float] = defaultdict(float)
    for a in ctx.acts:
        secs[a["key"]][a["day"]] += a["secs"]
        if a["key"] == "run":
            run_km[a["day"]] += (a.get("distance_m") or 0) / 1000

    def ctl(d: date) -> float | None:
        return _r((ctx.metrics.get(d.isoformat()) or {}).get("ctl"))

    def per_week(values: dict[date, float], d: date, scale: float) -> float:
        return round(sum(values.get(d - timedelta(days=i), 0.0) for i in range(28)) / 4 / scale, 1)

    def snapshot(race: date) -> dict[str, Any]:
        d = race - timedelta(days=days_to)
        return {"date": d.isoformat(), "ctl": ctl(d), "run_km": per_week(run_km, d, 1.0),
                **{f"h_{k}": per_week(secs[k], d, 3600.0) for k in ("run", "bike", "swim")}}

    offsets = list(range(-span, 1))
    series = []
    for a in editions:
        race = a["day"]
        series.append({"label": str(race.year), "name": a.get("name"), "date": race.isoformat(), "current": False,
                       "result": fmt.duration(a.get("duration_s")), "result_s": a.get("duration_s"),
                       "ctl_race": ctl(race - timedelta(days=1)),
                       "tsb_race": _r((ctx.metrics.get(race.isoformat()) or {}).get("tsb")),
                       "values": [ctl(race + timedelta(days=o)) for o in offsets], "same_point": snapshot(race)})
    series.append({"label": str(e.date.year), "name": e.name, "date": e.date.isoformat(), "current": True,
                   "values": [ctl(e.date + timedelta(days=o)) if e.date + timedelta(days=o) <= ctx.today else None
                              for o in offsets], "same_point": snapshot(e.date)})
    return {"event": e.name, "date": e.date.isoformat(), "days": days_to, "goal": e.goal, "sport": e.sport,
            "offsets": offsets,
            "series": series}


# -- recent activities ----------------------------------------------------------------------------------------------
def recent(ctx: Ctx, n: int = 14) -> list[dict[str, Any]]:
    out = []
    for a in reversed(ctx.acts[-n:]):
        km = (a.get("distance_m") or 0) / 1000
        if a["key"] == "swim":
            distance, pace = (f"{round(a['distance_m'])} m" if km else None), fmt.pace_per_100m(a.get("avg_speed_mps"))
        elif a["key"] == "run":
            distance, pace = (fmt.km(a["distance_m"], 2) if km else None), fmt.pace_per_km(a.get("avg_speed_mps"))
        else:
            distance = fmt.km(a["distance_m"]) if km else None
            pace = f"{a['avg_speed_mps'] * 3.6:.1f} km/h" if a["key"] == "bike" and a.get("avg_speed_mps") else None
        out.append({"id": a["activity_id"], "date": a["day"].isoformat(), "time": a["start_local"][11:16],
                    "sport": a["key"], "type": fmt.type_label(a.get("type_key")), "name": a.get("name"),
                    "duration": fmt.duration(a["secs"]), "distance": distance, "pace": pace,
                    "hr": a.get("avg_hr"), "power": None if not a.get("norm_power") else round(a["norm_power"]),
                    "load": round(a["load_v"]), "te": a.get("aerobic_te"),
                    "te_label": fmt.te_label(a.get("te_label")),
                    "decoupling": (ctx.fit.get(a["activity_id"]) or {}).get("dec")})
    return out
