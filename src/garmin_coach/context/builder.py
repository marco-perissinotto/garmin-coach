"""Assemble the ContextPack from the cache, the metrics and the owner's inputs (PLAN §8). No network, no LLM."""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..athlete import WEEKDAYS, Inputs
from ..clock import Clock, local_today, system_clock
from ..garmin.endpoints import REFRESH_TIERS
from ..metrics import compute, fitness, recovery, sport
from ..metrics import events as events_m
from ..metrics import load as load_m
from ..store import repo
from ..sync.jobs import active_jobs
from . import fmt

WEEKDAYS_FULL_IT = dict(zip(WEEKDAYS, ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
                            strict=True))
YESTERDAY_LOAD_FLAG = 2.0  # yesterday's load above this multiple of the chronic daily load
MONOTONY_FLAG = 2.0
ZONE_SPORT_IT = {"DEFAULT": "generali", "RUNNING": "corsa", "CYCLING": "bici", "SWIMMING": "nuoto"}


class ContextPack(BaseModel):
    """Everything the packs show, as structured data (also written as context.json)."""

    model_config = ConfigDict(extra="allow")
    generated: str
    today: str
    data_through: str | None = None
    cache: dict[str, str] = Field(default_factory=dict)
    stale_reason: str | None = None
    history_note: str | None = None
    athlete: dict[str, Any] = Field(default_factory=dict)
    history_md: str | None = None
    coach_notes_md: str | None = None
    readiness: dict[str, Any] | None = None
    sleep: dict[str, Any] | None = None
    hrv: dict[str, Any] | None = None
    rhr: dict[str, Any] | None = None
    body_battery: dict[str, Any] | None = None
    stress_yesterday: dict[str, Any] | None = None
    activities_today: list[dict[str, Any]] = Field(default_factory=list)
    activities_yesterday: list[dict[str, Any]] = Field(default_factory=list)
    load: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    days7: list[dict[str, Any]] = Field(default_factory=list)
    days28: list[dict[str, Any]] = Field(default_factory=list)
    weeks: list[dict[str, Any]] = Field(default_factory=list)
    zones7: dict[str, str] | None = None
    zones28: dict[str, str] | None = None
    efficiency: list[dict[str, Any]] = Field(default_factory=list)
    power_bests: dict[str, dict[str, int]] = Field(default_factory=dict)
    recent: list[dict[str, Any]] = Field(default_factory=list)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)


# -- derived layer freshness ----------------------------------------------------------------------------------
def _fingerprint(conn: sqlite3.Connection, inputs_mtime: float, today: date) -> str:
    q = conn.execute(
        "SELECT (SELECT COUNT(*) || '|' || COALESCE(MAX(updated_at), '') FROM activity), "
        "(SELECT COUNT(*) || '|' || COALESCE(MAX(updated_at), '') FROM daily), "
        "(SELECT COALESCE(MAX(updated_at), '') FROM sleep), (SELECT COALESCE(MAX(updated_at), '') FROM hrv), "
        "(SELECT COALESCE(MAX(taken_at), '') FROM profile_snapshot)"
    ).fetchone()
    return "#".join(str(x) for x in q) + f"#{inputs_mtime}#{today}"


def ensure_metrics(conn: sqlite3.Connection, inputs: Inputs, today: date, inputs_mtime: float = 0.0,
                   force: bool = False) -> bool:
    """Recompute metrics_daily only when the underlying data changed. Returns True when it recomputed."""
    fp = _fingerprint(conn, inputs_mtime, today)
    row = conn.execute("SELECT value FROM derived_state WHERE key='metrics_fp'").fetchone()
    if not force and row and row["value"] == fp:
        return False
    compute.recompute(conn, inputs.athlete, today)
    conn.execute("INSERT OR REPLACE INTO derived_state(key, value) VALUES ('metrics_fp', ?)", (fp,))
    return True


# -- helpers --------------------------------------------------------------------------------------------------
def _rows(conn: sqlite3.Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args)]


def _activity_view(a: dict[str, Any]) -> dict[str, Any]:
    fam = sport.family(a.get("type_key"))
    start = a.get("start_local") or ""
    v: dict[str, Any] = {
        "id": a["activity_id"],
        "date": start[:10],
        "time": start[11:16],
        "sport": fam,
        "type": fmt.type_label(a.get("type_key")),
        "name": a.get("name"),
        "duration": fmt.duration(a.get("moving_s") or a.get("duration_s")),
        "distance": None,
        "pace": None,
        "avg_hr": a.get("avg_hr"),
        "max_hr": a.get("max_hr"),
        "power": None,
        "load": None if a.get("training_load") is None else round(a["training_load"]),
        "te": None,
        "elev": None if not a.get("elev_gain_m") else round(a["elev_gain_m"]),
    }
    if fam == sport.SWIM:
        v["distance"] = f"{round(a['distance_m'])} m" if a.get("distance_m") else None
        v["pace"] = fmt.pace_per_100m((a.get("distance_m") or 0) / (a.get("moving_s") or a.get("duration_s") or 1))
    elif a.get("distance_m"):
        v["distance"] = fmt.km(a["distance_m"], 2 if fam == sport.RUN else 1)
        if fam == sport.RUN:
            v["pace"] = fmt.pace_per_km(a.get("avg_speed_mps"))
        elif fam == sport.BIKE and a.get("avg_speed_mps"):
            v["pace"] = f"{a['avg_speed_mps'] * 3.6:.1f} km/h"
    if a.get("avg_power"):
        np_ = f", NP {round(a['norm_power'])}" if a.get("norm_power") else ""
        v["power"] = f"{round(a['avg_power'])} W{np_}"
    if a.get("aerobic_te") is not None:
        label = fmt.te_label(a.get("te_label"))
        v["te"] = f"{a['aerobic_te']:.1f}/{(a.get('anaerobic_te') or 0):.1f}" + (f" {label}" if label else "")
    return v


def _short(acts: list[dict[str, Any]]) -> str:
    parts = []
    for a in acts:
        d = a["distance"] or a["duration"]
        parts.append(f"{a['type']} {d}")
    return "; ".join(parts) or "riposo"


# -- build ----------------------------------------------------------------------------------------------------
def build(conn: sqlite3.Connection, inputs: Inputs, *, clock: Clock = system_clock,
          freshness: Any | None = None, recent_n: int = 10) -> ContextPack:
    now = clock()
    today = local_today(now)
    yesterday = today - timedelta(days=1)
    pack = ContextPack(generated=now.astimezone().strftime("%Y-%m-%d %H:%M"), today=today.isoformat(),
                       problems=list(inputs.problems), history_md=inputs.history_md,
                       coach_notes_md=inputs.coach_notes_md)

    # header: cache ages, stale flag, history loading
    states = repo.tier_states(conn)
    for t in REFRESH_TIERS:
        last = states.get(t.value, {}).get("last_completed_at")
        pack.cache[t.value] = fmt.age_label(None if last is None else int((now - last).total_seconds() // 60))
    if freshness is not None and getattr(freshness, "stale", False):
        pack.stale_reason = freshness.stale_reason or "dati non aggiornati"
    if states.get("H", {}).get("last_completed_at") is None:
        job = next((j for j in active_jobs(conn) if j["kind"] == "backfill"), None)
        pack.history_note = (f"Storico ancora in caricamento (job #{job['id']}, {job['calls_done']}/"
                             f"{job['calls_planned']} chiamate)" if job else "Storico non ancora caricato")
    r = conn.execute("SELECT MAX(d) AS d FROM (SELECT MAX(date) AS d FROM daily WHERE rhr IS NOT NULL OR steps IS NOT "
                     "NULL UNION ALL SELECT MAX(date) FROM sleep UNION ALL SELECT MAX(substr(start_local,1,10)) "
                     "FROM activity)").fetchone()
    pack.data_through = r["d"] if r else None

    acts = compute.activity_rows(conn)
    zones = compute.hr_zones(conn)
    metrics = {m["date"]: m for m in _rows(conn, "SELECT * FROM metrics_daily WHERE date >= ?",
                                             (today - timedelta(days=120)).isoformat())}
    daily = {d["date"]: d for d in _rows(conn, "SELECT * FROM daily WHERE date >= ?",
                                         (today - timedelta(days=40)).isoformat())}
    sleeps = {s["date"]: s for s in _rows(conn, "SELECT * FROM sleep WHERE date >= ?",
                                          (today - timedelta(days=40)).isoformat())}
    hrvs = _rows(conn, "SELECT * FROM hrv WHERE date >= ? ORDER BY date", (today - timedelta(days=40)).isoformat())
    hrv_by = {h["date"]: h for h in hrvs}

    _athlete(pack, inputs, zones, acts, conn, today)

    # last night / today
    s = sleeps.get(today.isoformat())
    if s and s.get("total_s"):
        light = s.get("light_s")
        pack.sleep = {
            "duration": fmt.duration(s["total_s"]), "score": s.get("score"),
            "quality": fmt.sleep_quality(s.get("score_quality")),
            "deep": fmt.duration(s.get("deep_s")), "rem": fmt.duration(s.get("rem_s")), "light": fmt.duration(light),
            "awake": fmt.duration(s.get("awake_s")), "awake_count": s.get("awake_count"),
            "avg_hr": s.get("avg_hr"), "need": fmt.duration((s.get("sleep_need_min") or 0) * 60) if s.get(
                "sleep_need_min") else None,
            "deficit_min": None if not s.get("sleep_need_min") else round(s["sleep_need_min"] - s["total_s"] / 60),
            "start": (s.get("start_local") or "")[11:16], "end": (s.get("end_local") or "")[11:16],
            "avg7_score": _avg([sleeps.get((today - timedelta(days=i)).isoformat(), {}).get("score")
                                for i in range(7)]),
            "avg7_hours": _avg([(sleeps.get((today - timedelta(days=i)).isoformat(), {}).get("total_s") or 0) / 3600
                                or None for i in range(7)], 1),
        }
    hv = recovery.hrv_view([h for h in hrvs if h["date"] <= today.isoformat()][-7:])
    if hv and hrv_by.get(today.isoformat()):
        pack.hrv = {"last_night": hv.last_night, "weekly_avg": hv.weekly_avg,
                    "status": recovery.hrv_status_it(hv.status),
                    "low": hv.balanced_low, "high": hv.balanced_high, "nights_below": hv.nights_below,
                    "slope": None if hv.slope_7d is None else round(hv.slope_7d, 1)}
    dt, dy = daily.get(today.isoformat(), {}), daily.get(yesterday.isoformat(), {})
    rhr_avg = dt.get("rhr_7d_avg")
    if rhr_avg is None:
        prev = [v for i in range(1, 8) if (v := daily.get((today - timedelta(days=i)).isoformat(), {}).get("rhr"))]
        rhr_avg = round(statistics.fmean(prev)) if len(prev) >= 4 else None
    if dt.get("rhr"):
        pack.rhr = {"today": dt["rhr"], "avg7": rhr_avg, "delta": recovery.rhr_delta(dt["rhr"], rhr_avg)}
    if any(dt.get(k) is not None for k in ("bb_at_wake", "bb_high", "bb_low")):
        pack.body_battery = {"wake": dt.get("bb_at_wake"), "high": dt.get("bb_high"), "low": dt.get("bb_low"),
                             "charged": dt.get("bb_charged")}
    if dy.get("stress_avg") is not None:
        pack.stress_yesterday = {"avg": dy["stress_avg"], "high": fmt.duration(dy.get("stress_high_s")),
                                 "medium": fmt.duration(dy.get("stress_med_s")), "steps": dy.get("steps")}

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in acts:
        if a["start_local"][:10] >= (today - timedelta(days=40)).isoformat():
            by_day[a["start_local"][:10]].append(_activity_view(a))
    pack.activities_today = by_day.get(today.isoformat(), [])
    pack.activities_yesterday = by_day.get(yesterday.isoformat(), [])

    # readiness + load
    mt, my = metrics.get(today.isoformat()), metrics.get(yesterday.isoformat())
    if mt:
        reasons = json.loads(mt["readiness_reasons"]) if mt.get("readiness_reasons") else []
        rd = recovery.readiness(hv if pack.hrv else None, (pack.sleep or {}).get("score"),
                                (pack.body_battery or {}).get("wake"), (pack.rhr or {}).get("delta"),
                                mt.get("tsb"), (my or {}).get("ctl"))
        pack.readiness = {"score": mt.get("readiness_score"), "label": recovery.readiness_label(
            mt.get("readiness_score")), "reasons": reasons, "missing": rd.missing}
        last7 = [metrics.get((today - timedelta(days=i)).isoformat(), {}).get("load") or 0 for i in range(7)]
        last28 = [metrics.get((today - timedelta(days=i)).isoformat(), {}).get("load") or 0 for i in range(28)]
        ctl_prev = (my or {}).get("ctl")
        form = mt["tsb"] / ctl_prev if mt.get("tsb") is not None and ctl_prev else None
        y_load = (my or {}).get("load") or 0
        pack.load = {
            "atl": round(mt["atl"]), "ctl": round(mt["ctl"]), "tsb": None if mt.get("tsb") is None else round(mt["tsb"]),
            "form": form, "acwr": mt.get("acwr"), "acwr_flag": load_m.acwr_flag(mt.get("acwr")),
            "monotony": mt.get("monotony"), "load7": round(sum(last7)), "week_avg28": round(sum(last28) / 4),
            "yesterday": round(y_load), "yesterday_ratio": (y_load / ctl_prev) if ctl_prev else None,
        }

    for ev in events_m.event_views(inputs.events, today):
        e = ev.event
        pack.events.append({"name": e.name, "date": e.date.isoformat(), "days": ev.days_to, "weeks": ev.weeks_to,
                            "phase": ev.phase, "focus": ev.focus, "sport": e.sport, "distance_km": e.distance_km,
                            "goal": e.goal, "priority": e.priority, "notes": e.notes})

    _flags(pack)

    # tables
    for n, target in ((7, pack.days7), (28, pack.days28)):
        for i in range(n):
            d = today - timedelta(days=i)
            ds = d.isoformat()
            sl, m, dd = sleeps.get(ds, {}), metrics.get(ds, {}), daily.get(ds, {})
            target.append({"day": fmt.day_label(d), "load": None if m.get("load") is None else round(m["load"]),
                           "sleep_h": None if not sl.get("total_s") else round(sl["total_s"] / 3600, 1),
                           "sleep_score": sl.get("score"), "hrv": hrv_by.get(ds, {}).get("last_night_avg"),
                           "rhr": dd.get("rhr"), "bb": dd.get("bb_at_wake"), "stress": dd.get("stress_avg"),
                           "acts": _short(by_day.get(ds, []))})
    pack.weeks = _weeks(acts, metrics, today)
    recent7 = [a for a in acts if a["start_local"][:10] > (today - timedelta(days=7)).isoformat()]
    recent28 = [a for a in acts if a["start_local"][:10] > (today - timedelta(days=28)).isoformat()]
    for attr, sel in (("zones7", recent7), ("zones28", recent28)):
        zd = fitness.zone_distribution(sel)
        setattr(pack, attr, None if zd is None else {f"Z{k}": fmt.pct(v, signed=False) for k, v in zd.items()})
    th = pack.thresholds
    for ef in fitness.efficiency(acts, today, th.get("lthr_run"), th.get("lthr_bike")):
        pack.efficiency.append({"label": ef.label, "unit": ef.unit, "recent": None if ef.recent is None else round(
            ef.recent, 2), "previous": None if ef.previous is None else round(ef.previous, 2),
            "change": ef.change_pct, "n": f"{ef.n_recent}/{ef.n_previous}"})
    pack.power_bests = {k: {f"{d}s" if d < 60 else f"{d // 60}'": w for d, w in sorted(v.items())}
                        for k, v in fitness.power_bests(acts, today).items()}
    pack.recent = [_activity_view(a) for a in sorted(acts, key=lambda x: x["start_local"], reverse=True)[:recent_n]]
    return pack


def _avg(values: list[Any], decimals: int = 0) -> float | None:
    v = [x for x in values if x]
    if not v:
        return None
    return round(statistics.fmean(v), decimals) if decimals else round(statistics.fmean(v))


def _athlete(pack: ContextPack, inputs: Inputs, zones: list[dict[str, Any]], acts: list[dict[str, Any]],
             conn: sqlite3.Connection, today: date) -> None:
    a = inputs.athlete
    zd = {z.get("sport"): z for z in zones}
    default = compute.zone_for(zones)
    lthr_act = fitness.latest_activity_lthr(acts)
    det = conn.execute(
        "SELECT body FROM raw_payload WHERE endpoint='activity.detail' AND body LIKE '%functionalThresholdPower%' "
        "ORDER BY last_seen_at DESC LIMIT 1").fetchone()
    garmin_ftp = None
    if det:
        garmin_ftp = (json.loads(det["body"]).get("summaryDTO") or {}).get("functionalThresholdPower")
    pack.thresholds = {
        "max_hr": a.max_hr, "max_hr_garmin": default.get("maxHeartRateUsed"),
        "lthr": a.lthr or default.get("lactateThresholdHeartRateUsed"),
        "lthr_run": (zd.get("RUNNING") or {}).get("lactateThresholdHeartRateUsed") or a.lthr,
        "lthr_bike": (zd.get("CYCLING") or {}).get("lactateThresholdHeartRateUsed") or a.lthr,
        "lthr_activity": lthr_act[0] if lthr_act else None, "lthr_activity_date": lthr_act[1] if lthr_act else None,
        "ftp": a.ftp, "ftp_date": a.ftp_date.isoformat() if a.ftp_date else None,
        "ftp_garmin": None if garmin_ftp is None else round(garmin_ftp),
        "zones": [{"sport": ZONE_SPORT_IT.get(str(z.get("sport")), z.get("sport")), "method": z.get("trainingMethod"),
            "floors": [z.get(f"zone{i}Floor") for i in range(1, 6)], "max": z.get("maxHeartRateUsed")}
            for z in zones],
        "rest_hr_garmin": default.get("restingHeartRateUsed"),
    }
    weight_db = conn.execute("SELECT date, weight_g FROM weight ORDER BY ts_gmt DESC LIMIT 1").fetchone()
    weight = (f"{weight_db['weight_g'] / 1000:.1f} kg ({weight_db['date']}, Garmin)" if weight_db and weight_db[
        "weight_g"] else (f"{a.weight_kg:g} kg (inserito a mano, {a.weight_date})" if a.weight_kg else None))
    pack.athlete = {
        "name": a.name, "age": a.age_on(today), "sex": a.sex, "height_cm": a.height_cm, "weight": weight,
        "sports": a.sports, "constraints": a.constraints,
        "availability": [(WEEKDAYS_FULL_IT[d], a.availability.get(d)) for d in WEEKDAYS if d in a.availability],
        "availability_notes": a.availability_notes, "language": a.advice_language, "units": a.units,
    }


def _flags(pack: ContextPack) -> None:
    f = pack.flags
    if pack.sleep is None:
        f.append("Nessun dato di sonno per stanotte (orologio non ancora sincronizzato?)")
    elif (pack.sleep.get("score") or 100) < 60:
        f.append(f"Sonno scarso stanotte (punteggio {pack.sleep['score']})")
    elif (pack.sleep.get("deficit_min") or 0) >= 60:
        f.append(f"Sonno sotto il fabbisogno di {pack.sleep['deficit_min']} minuti")
    if pack.hrv:
        nb = pack.hrv.get("nights_below") or 0
        if nb >= 3:
            f.append(f"HRV sotto l'intervallo normale da {nb} notti")
        elif nb >= 1:
            f.append("HRV sotto l'intervallo normale stanotte")
    if pack.rhr and (pack.rhr.get("delta") or 0) >= recovery.RHR_DELTA_FLAG:
        f.append(f"FC a riposo +{pack.rhr['delta']} bpm sulla media di 7 giorni")
    if pack.body_battery and pack.body_battery.get("wake") is not None and pack.body_battery["wake"] < 50:
        f.append(f"Body Battery al risveglio bassa ({pack.body_battery['wake']})")
    if pack.load:
        ld = pack.load
        if ld.get("acwr_flag") == "alto":
            f.append(f"ACWR {ld['acwr']:.2f}: carico acuto molto sopra quello cronico")
        elif ld.get("acwr_flag") == "basso":
            f.append(f"ACWR {ld['acwr']:.2f}: carico acuto sotto quello cronico (scarico o pausa)")
        if ld.get("form") is not None and ld["form"] < -0.30:
            f.append(f"Forma (TSB) {fmt.pct(ld['form'])}: fatica accumulata")
        if ld.get("yesterday_ratio") and ld["yesterday_ratio"] >= YESTERDAY_LOAD_FLAG:
            f.append(f"Carico di ieri {ld['yesterday']} ({ld['yesterday_ratio']:.1f}× la media giornaliera cronica)")
        if ld.get("monotony") and ld["monotony"] >= MONOTONY_FLAG:
            f.append(f"Monotonia {ld['monotony']:.1f}: settimana troppo uniforme")
    for e in pack.events:
        if e["days"] <= 7:
            when = "oggi" if e["days"] == 0 else "domani" if e["days"] == 1 else f"tra {e['days']} giorni"
            f.append(f"Gara {when}: {e['name']} (priorità {e.get('priority') or '–'})")
    if pack.problems:
        f.append("File personali: " + "; ".join(pack.problems))


def _weeks(acts: list[dict[str, Any]], metrics: dict[str, dict[str, Any]], today: date,
           n: int = 4) -> list[dict[str, Any]]:
    monday = today - timedelta(days=today.weekday())
    out = []
    for w in range(n):
        start = monday - timedelta(weeks=w)
        end = start + timedelta(days=6)
        sel = [a for a in acts if start.isoformat() <= a["start_local"][:10] <= end.isoformat()]
        by: dict[str, dict[str, float]] = defaultdict(lambda: {"s": 0.0, "m": 0.0, "load": 0.0, "elev": 0.0, "n": 0})
        for a in sel:
            b = by[sport.family(a.get("type_key"))]
            b["s"] += a.get("moving_s") or a.get("duration_s") or 0
            b["m"] += a.get("distance_m") or 0
            b["load"] += a.get("training_load") or 0
            b["elev"] += a.get("elev_gain_m") or 0
            b["n"] += 1
        week_load = sum((metrics.get((start + timedelta(days=i)).isoformat(), {}).get("load") or 0) for i in range(7))
        out.append({
            "label": f"{fmt.day_label(start)}–{fmt.day_label(min(end, today))}" + (" (in corso)" if w == 0 else ""),
            "load": round(week_load), "time": fmt.hours(sum(b["s"] for b in by.values())),
            "sports": [{"sport": k, "n": int(v["n"]), "time": fmt.hours(v["s"]),
                        "distance": (f"{round(v['m'])} m" if k == sport.SWIM else fmt.km(v["m"])) if v["m"] else "–",
                        "elev": round(v["elev"]) or None} for k, v in sorted(by.items(), key=lambda kv: -kv[1]["s"])],
        })
    return out


def inputs_mtime(paths_config: Any) -> float:
    """Latest modification time of the owner's files (the metrics depend on athlete.yaml)."""
    try:
        return max((p.stat().st_mtime for p in paths_config.glob("*") if p.is_file()), default=0.0)
    except OSError:
        return 0.0


def now_local(clock: Clock = system_clock) -> datetime:
    return clock().astimezone()
