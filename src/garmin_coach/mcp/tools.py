"""MCP tool logic (PLAN §9): every data tool passes through the freshness gate, then reads SQLite.

Tools run in worker threads (the SDK calls sync functions via ``anyio.to_thread``), so each call opens its own SQLite
connection and builds its own gate; the refresh lock and the process-wide background guard keep refreshes single.
Garmin is never called from here except through the gate, plus the lazy detail fetch of one activity (2 calls).

Every method returns a JSON-able dict and never raises: bad input or an internal error becomes ``{"error": ...}``.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic import ValidationError

from .. import state as state_m
from ..app import App
from ..athlete import load_inputs
from ..clock import local_today
from ..context import builder, render
from ..garmin.chunking import parse_range_key
from ..garmin.endpoints import REFRESH_TIERS, SYNC_TIERS, Tier
from ..metrics import compute, sport
from ..metrics import events as events_m
from ..secrets import key_file_status, redact
from ..settings import STATE_FILE
from ..status import build_status
from ..store import repo
from ..sync import jobs as jobs_mod
from ..sync.freshness import Freshness

log = logging.getLogger(__name__)

MAX_PAYLOAD_CHARS = 60_000  # ~15k tokens: beyond this a tool answers with an error asking for a narrower request
MAX_DAY_RANGE = 92  # get_range_summary(granularity=day), get_training_load
MAX_WEEK_RANGE = 731  # get_range_summary(granularity=week), get_weight
MAX_ACTIVITIES = 100
MAX_SPLITS = 40
CURVE_POINTS = 96  # Body Battery curve: one point per 15 min
HR_SAMPLE_MINUTES = 10  # heart-rate samples are averaged into 10-minute buckets
MAX_NOTE_CHARS = 500
REFRESH_MIN_INTERVAL = timedelta(minutes=5)  # refresh(): once per tier per 5 min, even when forced (PLAN §9)
PROFILE_SECTIONS = ("profile", "zones", "history", "coach_notes")
SPORT_ALIASES = {
    "run": sport.RUN, "running": sport.RUN, "corsa": sport.RUN,
    "bike": sport.BIKE, "cycling": sport.BIKE, "ride": sport.BIKE, "bici": sport.BIKE,
    "swim": sport.SWIM, "swimming": sport.SWIM, "nuoto": sport.SWIM,
    "strength": sport.STRENGTH, "forza": sport.STRENGTH, "multisport": sport.MULTI, "triathlon": sport.MULTI,
    "other": sport.OTHER, "altro": sport.OTHER,
}
_ACTIVITY_HIDDEN = ("summary_hash", "has_fit", "updated_at", "vo2max")  # vo2max: always empty on this API (D-040)
_SPLIT_DROP = re.compile(r"(?i)latitude|longitude")


class InputError(ValueError):
    """Invalid tool arguments: reported to the model as ``{"error": ...}``."""


def _safe[F: Callable[..., dict[str, Any]]](fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except InputError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a tool must answer, never crash the server
            log.exception("MCP tool %s failed", fn.__name__)
            return {"error": redact(f"internal error in {fn.__name__}: {type(exc).__name__}: {exc}")}

    return wrapper  # type: ignore[return-value]


# -- argument parsing ---------------------------------------------------------------------------------------------
def _date(value: str | None, name: str, default: date | None = None) -> date:
    if value is None or value == "":
        if default is None:
            raise InputError(f"{name} is required (YYYY-MM-DD)")
        return default
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise InputError(f"{name} must be a date as YYYY-MM-DD, got {value!r}") from None


def _range(start: str | None, end: str | None, today: date, max_days: int, default_days: int) -> tuple[date, date]:
    e = _date(end, "end", today)
    s = _date(start, "start", e - timedelta(days=default_days - 1))
    if s > e:
        raise InputError("start must not be after end")
    if (e - s).days + 1 > max_days:
        raise InputError(f"range too long: at most {max_days} days per request")
    return s, e


def _max_age(minutes: int | None) -> int | None:
    if minutes is None:
        return None
    if not isinstance(minutes, int) or minutes < 0 or minutes > 60 * 24 * 30:
        raise InputError("max_age_minutes must be an integer between 0 (force refresh) and 43200")
    return minutes


def _days(a: date, b: date) -> Iterator[date]:
    for i in range((b - a).days + 1):
        yield a + timedelta(days=i)


def _clean(d: dict[str, Any] | None, drop: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if d is None:
        return None
    return {k: v for k, v in d.items() if v is not None and k not in drop}


def _r(v: float | None, nd: int = 1) -> float | None:
    return None if v is None else round(v, nd)


def _mean(values: list[float | None], nd: int = 1) -> float | None:
    v = [x for x in values if x is not None]
    return round(sum(v) / len(v), nd) if v else None


class CoachService:
    """The tool implementations; ``mcp/server.py`` registers them with their descriptions for the model."""

    def __init__(self, app: App) -> None:
        self.app = app
        self._note_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        _ = app.jobs  # create the job manager now, not concurrently from two tool threads

    # -- plumbing -------------------------------------------------------------------------------------------------
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = self.app.connect()
        try:
            yield conn
        finally:
            conn.close()

    def _today(self) -> date:
        return local_today(self.app.clock())

    def _fresh(self, conn: sqlite3.Connection, tiers: set[Tier] | frozenset[Tier], max_age_minutes: int | None,
               *, background: bool = True) -> Freshness:
        if max_age_minutes is None:
            ma = None
        elif max_age_minutes == 0 and tiers & SYNC_TIERS:
            # "Force" reloads what the answer is about (activities, today's wellness); the background tiers keep
            # their TTL instead of costing another ~15 calls (D-052).
            ma = {t: timedelta(0) for t in tiers & SYNC_TIERS}
        else:
            ma = {t: timedelta(minutes=max_age_minutes) for t in tiers}
        return self.app.gate(background=background, conn=conn).ensure_fresh(set(tiers), ma, trigger="mcp")

    def _day_tiers(self, d: date) -> set[Tier]:
        """Today/yesterday come from tier B (single-day endpoints); older days from the 7-day range refresh (C)."""
        today = self._today()
        if d > today:
            raise InputError(f"{d} is in the future")
        return {Tier.B} if d >= today - timedelta(days=1) else {Tier.C}

    def _reply(self, fr: Freshness | None, **data: Any) -> dict[str, Any]:
        out = dict(data)
        if fr is not None:
            out["freshness"] = fr.model_dump(exclude_defaults=True)
            out["freshness"].setdefault("history_loaded", fr.history_loaded)
            out["freshness"].setdefault("stale", fr.stale)
        key = key_file_status()
        if key.source == "missing":
            out["error"] = (f"Garmin API key not found at {key.path}: run `garmin-coach doctor`. "
                            "Answers come from the local cache only.")
        size = len(json.dumps(out, default=str))
        if size > MAX_PAYLOAD_CHARS:
            return {"error": f"response too large ({size} characters): ask for a shorter range or fewer details",
                    "freshness": out.get("freshness")}
        return out

    def _pack(self, conn: sqlite3.Connection, fr: Freshness | None, day: date | None = None) -> builder.ContextPack:
        """The ContextPack as of ``day`` (default today). Metrics are always (re)computed for the real today; the
        coaching state is current, so a past-date pack leaves it out."""
        today = self._today()
        inputs = load_inputs(self.app.paths, today)
        builder.ensure_metrics(conn, inputs, today, builder.inputs_mtime(self.app.paths.config_dir))
        clock = self.app.clock
        past = False
        if day is not None and day != today:
            past = True
            pinned = datetime.combine(day, time(23, 59)).astimezone().astimezone(UTC)
            clock = lambda: pinned  # noqa: E731
        ctx = self.app.settings.context
        return builder.build(conn, inputs, clock=clock, freshness=fr, recent_n=ctx.recent_activities,
                             include_state=not past, state_stale_days=ctx.state_stale_days)

    def _ensure_metrics(self, conn: sqlite3.Connection) -> None:
        inputs = load_inputs(self.app.paths)
        builder.ensure_metrics(conn, inputs, self._today(), builder.inputs_mtime(self.app.paths.config_dir))

    # -- packs ----------------------------------------------------------------------------------------------------
    def pack_texts(self, aliases: tuple[str, ...], max_age_minutes: int | None = None) -> tuple[dict[str, str],
                                                                                                   Freshness]:
        """Rendered Markdown packs (used by get_context_pack, the resources and the prompts)."""
        tiers: set[Tier] = set()
        for a in aliases:
            tiers |= render.PACK_TIERS[a]
        with self._conn() as conn:
            fr = self._fresh(conn, tiers, max_age_minutes)
            pack = self._pack(conn, fr)
            names = [n for a in aliases for n in render.ALIASES[a]]
            return {n: render.render(pack, n) for n in names}, fr

    @_safe
    def get_daily_brief(self, date_: str | None = None, max_age_minutes: int | None = None) -> dict[str, Any]:
        today = self._today()
        d = _date(date_, "date", today)
        if d > today:
            raise InputError(f"{d} is in the future")
        tiers = set(render.PACK_TIERS["daily"]) if d >= today - timedelta(days=1) else {Tier.C}
        with self._conn() as conn:
            fr = self._fresh(conn, tiers, _max_age(max_age_minutes))
            p = self._pack(conn, fr, d)
        brief = {
            "date": p.today, "data_through": p.data_through, "history_note": p.history_note, "flags": p.flags,
            "readiness_heuristic": p.readiness, "sleep_last_night": p.sleep, "hrv_last_night": p.hrv,
            "resting_hr": p.rhr, "body_battery": p.body_battery, "stress_yesterday": p.stress_yesterday,
            "activities_today": p.activities_today, "activities_yesterday": p.activities_yesterday,
            "training_load": p.load, "next_events": p.events[:3], "coaching_state": p.coaching_state,
            "problems": p.problems,
        }
        return self._reply(fr, **{k: v for k, v in brief.items() if v not in (None, [], {})})

    @_safe
    def get_context_pack(self, name: str, max_age_minutes: int | None = None) -> dict[str, Any]:
        by_file = {n: a for a, names in render.ALIASES.items() if len(names) == 1 for n in names}
        alias = name if name in render.ALIASES else by_file.get(name)
        if alias is None:
            raise InputError(f"unknown pack {name!r}; choose one of: {', '.join(render.ALIASES)}")
        texts, fr = self.pack_texts((alias,), _max_age(max_age_minutes))
        return self._reply(fr, name=alias, markdown="\n\n".join(texts.values()))

    # -- profile, events, knowledge -------------------------------------------------------------------------------
    @_safe
    def get_events(self) -> dict[str, Any]:
        inputs = load_inputs(self.app.paths)
        today = self._today()
        with self._conn() as conn:
            fr = self._fresh(conn, set(), None)
        events = []
        for ev in events_m.event_views(inputs.events, today):
            e = ev.event
            events.append(_clean({
                "name": e.name, "date": e.date.isoformat(), "days_to_go": ev.days_to, "weeks_to_go": ev.weeks_to,
                "phase": ev.phase, "suggested_focus": ev.focus, "sport": e.sport, "distance_km": e.distance_km,
                "elevation_m": e.elevation_m, "goal": e.goal, "priority": e.priority, "notes": e.notes,
            }))
        problems = [p for p in inputs.problems if "events" in p]
        return self._reply(fr, today=today.isoformat(), events=events, **({"problems": problems} if problems else {}))

    @_safe
    def get_athlete_profile(self, sections: list[str] | None = None) -> dict[str, Any]:
        wanted = list(sections or PROFILE_SECTIONS)
        bad = [s for s in wanted if s not in PROFILE_SECTIONS]
        if bad:
            raise InputError(f"unknown sections {bad}; choose among {list(PROFILE_SECTIONS)}")
        inputs = load_inputs(self.app.paths)
        out: dict[str, Any] = {}
        fr = None
        if "profile" in wanted or "zones" in wanted:
            with self._conn() as conn:
                fr = self._fresh(conn, render.PACK_TIERS["athlete"], None)
                p = self._pack(conn, fr)
            th = dict(p.thresholds)
            zones = th.pop("zones", [])
            if "profile" in wanted:
                out["profile"] = {**p.athlete, "thresholds": _clean(th)}
            if "zones" in wanted:
                out["hr_zones"] = zones
        if "history" in wanted:
            out["history_md"] = inputs.history_md or "history.md not found: no athletic history recorded yet."
        if "coach_notes" in wanted:
            out["coach_notes_md"] = inputs.coach_notes_md or "coach-notes.md not found: no agreed rules yet."
        if inputs.problems:
            out["problems"] = inputs.problems
        return self._reply(fr, **out)

    @_safe
    def append_coach_note(self, text: str) -> dict[str, Any]:
        note = " ".join(redact(text or "").split())
        if len(note) < 3:
            raise InputError("the note is empty")
        if len(note) > MAX_NOTE_CHARS:
            raise InputError(f"the note is too long ({len(note)} characters, max {MAX_NOTE_CHARS}): summarise it")
        note = note.lstrip("-* ").strip()
        line = f"- {self._today().isoformat()} {note}"
        path = self.app.paths.config_dir / "coach-notes.md"
        with self._note_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            sep = "" if not existing or existing.endswith("\n") else "\n"
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"{sep}{line}\n")
        log.info("coach note appended (%d chars)", len(note))
        return {"appended": line, "file": "coach-notes.md"}

    # -- coaching state (local file only, never touches Garmin) ----------------------------------------------------
    def _state_view(self, state: state_m.CoachingState | None, problems: list[str]) -> dict[str, Any]:
        return state_m.view(state, problems, self._today(), self.app.settings.context.state_stale_days)

    @_safe
    def get_coaching_state(self) -> dict[str, Any]:
        return self._state_view(*state_m.load_state(self.app.paths, self._today()))

    @_safe
    def update_coaching_state(self, symptoms: list[dict[str, Any]] | None = None, plan: dict[str, Any] | None = None,
                              decisions: dict[str, Any] | None = None, confirm: bool = False) -> dict[str, Any]:
        """Per-section replace (D-061): a passed section replaces the stored one, an empty one clears it, an omitted
        one is kept; ``confirm`` alone re-dates an unchanged state."""
        passed = {k: v for k, v in (("symptoms", symptoms), ("plan", plan), ("decisions", decisions))
                  if v is not None}
        if not passed and not confirm:
            raise InputError("nothing to update: pass at least one section or confirm=true")
        paths = self.app.paths
        with self._state_lock:
            existing, problems = state_m.load_state(paths, self._today())
            if problems and len(passed) < len(state_m.SECTIONS):
                detail = problems[0].removeprefix(f"{STATE_FILE} non valido ").strip("()")
                raise InputError(f"{STATE_FILE} cannot be read ({detail}): fix it by hand, or pass all three "
                                 "sections to replace it")
            if existing is None and not passed:
                raise InputError("no coaching state to confirm")
            data = existing.model_dump(by_alias=True, exclude={"updated", "updated_by"}) if existing else {}
            data.update(passed)
            if not data.get("plan"):
                data["plan"] = None
            try:
                new = state_m.CoachingState.model_validate(data)
                saved = state_m.save_state(paths, new, self.app.clock(), past_window=passed)
            except ValidationError as exc:
                raise InputError(state_m.validation_message(exc)) from None
            except state_m.StateError as exc:
                raise InputError(str(exc)) from None
        log.info("coaching state updated: symptoms=%d plan_days=%d open=%d taken=%d", len(saved.symptoms),
                 len(saved.plan.days) if saved.plan else 0, len(saved.decisions.open), len(saved.decisions.taken))
        return {**self._state_view(saved, []), "changed": list(passed) or ["confirmed"]}

    def knowledge_text(self, name: str) -> str:
        """Resources garmin-coach://knowledge/{history,coach-notes,coaching-state}: the owner's file verbatim (no
        refresh)."""
        path = self.app.paths.config_dir / (STATE_FILE if name == "coaching-state" else f"{name}.md")
        if not path.exists():
            return f"_{path.name} not found in the garmin-coach config folder._\n"
        return path.read_text(encoding="utf-8")

    # -- ranges ---------------------------------------------------------------------------------------------------
    def _range_tiers(self, conn: sqlite3.Connection, s: date, e: date) -> tuple[set[Tier], bool]:
        """(tiers, synchronous?) for a date range: C, synchronous when the recent part of the range has no cached
        data at all (PLAN §6.5 table)."""
        today = self._today()
        recent_start = today - timedelta(days=6)
        if e < recent_start:
            return {Tier.C}, False
        row = conn.execute("SELECT 1 FROM daily WHERE date BETWEEN ? AND ? LIMIT 1",
                           (max(s, recent_start).isoformat(), e.isoformat())).fetchone()
        return {Tier.C}, row is None

    @_safe
    def get_range_summary(self, start: str | None = None, end: str | None = None, granularity: str = "day",
                          max_age_minutes: int | None = None) -> dict[str, Any]:
        if granularity not in ("day", "week"):
            raise InputError("granularity must be 'day' or 'week'")
        today = self._today()
        s, e = _range(start, end, today, MAX_DAY_RANGE if granularity == "day" else MAX_WEEK_RANGE,
                      7 if granularity == "day" else 28)
        with self._conn() as conn:
            tiers, sync = self._range_tiers(conn, s, e)
            fr = self._fresh(conn, tiers, _max_age(max_age_minutes), background=not sync)
            self._ensure_metrics(conn)
            days = self._day_rows(conn, s, e)
        if granularity == "day":
            return self._reply(fr, start=s.isoformat(), end=e.isoformat(), granularity="day", days=days)
        return self._reply(fr, start=s.isoformat(), end=e.isoformat(), granularity="week", weeks=self._weeks(days))

    def _day_rows(self, conn: sqlite3.Connection, s: date, e: date) -> list[dict[str, Any]]:
        a, b = s.isoformat(), e.isoformat()

        def by_date(sql: str) -> dict[str, dict[str, Any]]:
            return {r["date"]: dict(r) for r in conn.execute(sql, (a, b))}

        met = by_date("SELECT * FROM metrics_daily WHERE date BETWEEN ? AND ?")
        dly = by_date("SELECT * FROM daily WHERE date BETWEEN ? AND ?")
        slp = by_date("SELECT * FROM sleep WHERE date BETWEEN ? AND ?")
        hrv = by_date("SELECT * FROM hrv WHERE date BETWEEN ? AND ?")
        acts: dict[str, list[dict[str, Any]]] = {}
        for r in conn.execute("SELECT * FROM activity WHERE substr(start_local,1,10) BETWEEN ? AND ? "
                              "ORDER BY start_local", (a, b)):
            acts.setdefault(r["start_local"][:10], []).append(dict(r))
        out = []
        for d in _days(s, e):
            k = d.isoformat()
            m, dd, sl, hv = met.get(k, {}), dly.get(k, {}), slp.get(k, {}), hrv.get(k, {})
            row = {
                "date": k, "load": _r(m.get("load"), 0), "atl": _r(m.get("atl"), 0), "ctl": _r(m.get("ctl"), 0),
                "tsb": _r(m.get("tsb"), 0), "acwr": _r(m.get("acwr"), 2), "readiness": m.get("readiness_score"),
                "sleep_h": _r((sl.get("total_s") or 0) / 3600) or None, "sleep_score": sl.get("score"),
                "hrv_ms": hv.get("last_night_avg"), "rhr_bpm": dd.get("rhr"), "bb_wake": dd.get("bb_at_wake"),
                "stress_avg": dd.get("stress_avg"), "steps": dd.get("steps"),
                "activities": [_clean({
                    "id": x["activity_id"], "sport": sport.family(x["type_key"]), "type_key": x["type_key"],
                    "duration_min": _r((x.get("moving_s") or x.get("duration_s") or 0) / 60, 0),
                    "distance_km": _r((x.get("distance_m") or 0) / 1000, 2) or None,
                    "load": _r(x.get("training_load"), 0), "avg_hr": x.get("avg_hr"),
                }) for x in acts.get(k, [])],
            }
            out.append(_clean(row) or {})
        return out

    @staticmethod
    def _weeks(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
        weeks: dict[str, list[dict[str, Any]]] = {}
        for d in days:
            day = date.fromisoformat(d["date"])
            weeks.setdefault((day - timedelta(days=day.weekday())).isoformat(), []).append(d)
        out = []
        for monday, ds in weeks.items():
            by_sport: dict[str, dict[str, float]] = {}
            for d in ds:
                for a in d.get("activities", []):
                    b = by_sport.setdefault(a["sport"], {"n": 0, "time_h": 0.0, "distance_km": 0.0, "load": 0.0})
                    b["n"] += 1
                    b["time_h"] += (a.get("duration_min") or 0) / 60
                    b["distance_km"] += a.get("distance_km") or 0
                    b["load"] += a.get("load") or 0
            out.append(_clean({
                "week_start": monday, "days": len(ds), "load": round(sum(d.get("load") or 0 for d in ds)),
                "ctl_end": ds[-1].get("ctl"), "tsb_end": ds[-1].get("tsb"),
                "sleep_h_avg": _mean([d.get("sleep_h") for d in ds]),
                "sleep_score_avg": _mean([d.get("sleep_score") for d in ds], 0),
                "hrv_ms_avg": _mean([d.get("hrv_ms") for d in ds], 0), "rhr_bpm_avg": _mean([d.get("rhr_bpm") for d in ds]),
                "stress_avg": _mean([d.get("stress_avg") for d in ds], 0),
                "by_sport": {k: {"n": int(v["n"]), "time_h": round(v["time_h"], 1),
                                 "distance_km": round(v["distance_km"], 1), "load": round(v["load"])}
                             for k, v in sorted(by_sport.items(), key=lambda kv: -kv[1]["time_h"])},
            }) or {})
        return out

    @_safe
    def get_training_load(self, start: str | None = None, end: str | None = None,
                          max_age_minutes: int | None = None) -> dict[str, Any]:
        s, e = _range(start, end, self._today(), MAX_DAY_RANGE, 42)
        with self._conn() as conn:
            fr = self._fresh(conn, {Tier.A}, _max_age(max_age_minutes))
            self._ensure_metrics(conn)
            rows = [_clean({
                "date": r["date"], "load": _r(r["load"], 0), "atl": _r(r["atl"], 1), "ctl": _r(r["ctl"], 1),
                "tsb": _r(r["tsb"], 1), "acwr": _r(r["acwr"], 2), "monotony": _r(r["monotony"], 2),
                "strain": _r(r["strain"], 0), "readiness": r["readiness_score"],
            }) for r in conn.execute("SELECT * FROM metrics_daily WHERE date BETWEEN ? AND ? ORDER BY date",
                                     (s.isoformat(), e.isoformat()))]
        return self._reply(fr, start=s.isoformat(), end=e.isoformat(), series=rows)

    # -- activities -----------------------------------------------------------------------------------------------
    @_safe
    def list_activities(self, start: str | None = None, end: str | None = None, type_: str | None = None,
                        limit: int = 20, max_age_minutes: int | None = None) -> dict[str, Any]:
        if not isinstance(limit, int) or not 1 <= limit <= MAX_ACTIVITIES:
            raise InputError(f"limit must be between 1 and {MAX_ACTIVITIES}")
        today = self._today()
        s = _date(start, "start", date(1990, 1, 1))
        e = _date(end, "end", today)
        if s > e:
            raise InputError("start must not be after end")
        fam = SPORT_ALIASES.get((type_ or "").strip().lower())
        with self._conn() as conn:
            fr = self._fresh(conn, {Tier.A}, _max_age(max_age_minutes))
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM activity WHERE substr(start_local,1,10) BETWEEN ? AND ? ORDER BY start_local DESC",
                (s.isoformat(), e.isoformat()))]
        if type_:
            key = type_.strip().lower()
            rows = [r for r in rows if (sport.family(r["type_key"]) == fam if fam else r["type_key"] == key)]
        out = []
        for r in rows[:limit]:
            v = builder._activity_view(r)
            v.update(type_key=r["type_key"], has_detail=bool(r["has_detail"]))
            out.append(_clean(v))
        return self._reply(fr, count=len(out), more=len(rows) > limit, activities=out)

    def _lazy_detail(self, conn: sqlite3.Connection, activity_id: int) -> str | None:
        """Fetch detail + workouts for one activity (2 calls, synchronous). Returns a problem description or None.

        Runs with an empty tier set so tier A's freshness is not touched by a single-activity fetch."""
        from ..sync.lock import RefreshLock
        from ..sync.planner import Planner
        from ..sync.runner import Runner

        app, s = self.app, self.app.settings
        now = app.clock()
        cd = repo.tier_states(conn).get(Tier.A.value, {}).get("cooldown_until")
        if cd and cd > now:
            return f"Garmin in cooldown until {cd.isoformat()}: detail not fetched"
        calls = Planner(conn, s, now).lazy_detail_calls(activity_id)
        if not calls:
            return None
        lock = RefreshLock(app.paths.lock)
        if not lock.acquire(s.sync.lock_wait_seconds):
            return "another refresh is running: detail not fetched, ask again in a minute"
        try:
            fetcher = app.fetcher(len(calls), s.sync.sync_timeout_seconds)
            try:
                res = Runner(conn, s, fetcher, clock=app.clock).run(set(), calls=calls)
            finally:
                fetcher.close()
        except Exception as exc:  # noqa: BLE001 - the tool still answers from the list row
            return redact(f"detail fetch failed: {exc}")
        finally:
            lock.release()
        return None if res.status == "done" else f"detail fetch {res.status}: {res.error or ''}".strip()

    @_safe
    def get_activity(self, activity_id: int, include_splits: bool = False,
                     max_age_minutes: int | None = None) -> dict[str, Any]:
        if not isinstance(activity_id, int) or activity_id <= 0:
            raise InputError("activity_id must be a positive integer (see list_activities)")
        with self._conn() as conn:
            fr = self._fresh(conn, {Tier.A}, _max_age(max_age_minutes))
            row = conn.execute("SELECT * FROM activity WHERE activity_id=?", (activity_id,)).fetchone()
            if row is None:
                return self._reply(fr, error=f"activity {activity_id} is not in the local cache (see list_activities)")
            problem = None if row["has_detail"] else self._lazy_detail(conn, activity_id)
            row = dict(conn.execute("SELECT * FROM activity WHERE activity_id=?", (activity_id,)).fetchone())
            detail = repo.latest_raw(conn, "activity.detail", str(activity_id))
            workouts = repo.latest_raw(conn, "activity.workouts", str(activity_id))
        out: dict[str, Any] = {
            "activity": _clean(row, _ACTIVITY_HIDDEN),
            "view": _clean(builder._activity_view(row)),
        }
        if isinstance(detail, dict):
            summ = detail.get("summaryDTO") or {}
            out["detail"] = _clean({
                "description": detail.get("description"),
                **{k: summ.get(k) for k in (
                    "trainingStressScore", "intensityFactor", "functionalThresholdPower", "normalizedPower",
                    "maxPowerTwentyMinutes", "avgRespirationRate", "averageTemperature",
                    "moderateIntensityMinutes", "vigorousIntensityMinutes", "beginPotentialStamina",
                    "endPotentialStamina", "minAvailableStamina", "aerobicTrainingEffectMessage",
                    "anaerobicTrainingEffectMessage")},
            })
            if include_splits:
                splits = detail.get("splitSummaries") or []
                out["splits"] = [{k: v for k, v in sp.items() if v is not None and not _SPLIT_DROP.search(k)}
                                 for sp in splits[:MAX_SPLITS] if isinstance(sp, dict)]
        if isinstance(workouts, list) and workouts:
            out["workouts"] = [_clean({
                "name": w.get("workoutName"), "sport": w.get("sport"),
                "steps": [_clean({k: st.get(k) for k in (
                    "stepIndex", "intensity", "durationType", "durationValue", "targetType", "targetValueLow",
                    "targetValueHigh", "name", "notes")}) for st in (w.get("steps") or []) if isinstance(st, dict)],
            }) for w in workouts if isinstance(w, dict)]
        if problem:
            out["detail_problem"] = problem
        return self._reply(fr, **out)

    # -- single-day wellness --------------------------------------------------------------------------------------
    def _wellness(self, date_: str | None, max_age_minutes: int | None,
                  read: Callable[[sqlite3.Connection, date], dict[str, Any]]) -> dict[str, Any]:
        d = _date(date_, "date", self._today())
        tiers = self._day_tiers(d)
        with self._conn() as conn:
            fr = self._fresh(conn, tiers, _max_age(max_age_minutes))
            data = read(conn, d)
        return self._reply(fr, date=d.isoformat(), **data)

    @_safe
    def get_sleep(self, date_: str | None = None, max_age_minutes: int | None = None) -> dict[str, Any]:
        def read(conn: sqlite3.Connection, d: date) -> dict[str, Any]:
            r = conn.execute("SELECT * FROM sleep WHERE date=?", (d.isoformat(),)).fetchone()
            if r is None or not r["total_s"]:
                return {"sleep": None, "note": "no sleep data for this night (watch not synced yet?)"}
            return {"sleep": _clean(dict(r), ("updated_at", "date"))}

        return self._wellness(date_, max_age_minutes, read)

    @_safe
    def get_hrv(self, date_: str | None = None, max_age_minutes: int | None = None) -> dict[str, Any]:
        def read(conn: sqlite3.Connection, d: date) -> dict[str, Any]:
            r = conn.execute("SELECT * FROM hrv WHERE date=?", (d.isoformat(),)).fetchone()
            last7 = [{"date": x["date"], "ms": x["last_night_avg"]} for x in conn.execute(
                "SELECT date, last_night_avg FROM hrv WHERE date BETWEEN ? AND ? ORDER BY date",
                ((d - timedelta(days=6)).isoformat(), d.isoformat()))]
            out: dict[str, Any] = {"hrv": _clean(dict(r), ("updated_at", "date")) if r else None,
                                   "last_7_nights": last7}
            if r is None:
                out["note"] = "no HRV data for this night"
            return out

        return self._wellness(date_, max_age_minutes, read)

    @_safe
    def get_body_battery(self, date_: str | None = None, include_curve: bool = False,
                         max_age_minutes: int | None = None) -> dict[str, Any]:
        def read(conn: sqlite3.Connection, d: date) -> dict[str, Any]:
            r = conn.execute("SELECT bb_high, bb_low, bb_at_wake, bb_charged, bb_drained FROM daily WHERE date=?",
                             (d.isoformat(),)).fetchone()
            out: dict[str, Any] = {"body_battery": _clean(dict(r)) if r else None}
            if not out["body_battery"]:
                out["note"] = "no Body Battery data for this day"
            if include_curve:
                out["curve"] = _bb_curve(conn, d)
            return out

        return self._wellness(date_, max_age_minutes, read)

    @_safe
    def get_heart_rate(self, date_: str | None = None, include_samples: bool = False,
                       max_age_minutes: int | None = None) -> dict[str, Any]:
        def read(conn: sqlite3.Connection, d: date) -> dict[str, Any]:
            r = conn.execute("SELECT rhr, rhr_7d_avg, hr_min FROM daily WHERE date=?", (d.isoformat(),)).fetchone()
            out: dict[str, Any] = {
                "heart_rate": _clean({"resting_bpm": r["rhr"], "resting_7d_avg_bpm": r["rhr_7d_avg"],
                                      "min_bpm": r["hr_min"]}) if r else None,
                "note": "the daily maximum is not reported: Garmin's value is often an optical artefact",
            }
            if include_samples:
                out["samples_10min"] = _hr_samples(conn, d)
            return out

        return self._wellness(date_, max_age_minutes, read)

    @_safe
    def get_weight(self, start: str | None = None, end: str | None = None,
                   max_age_minutes: int | None = None) -> dict[str, Any]:
        s, e = _range(start, end, self._today(), MAX_WEEK_RANGE, 90)
        with self._conn() as conn:
            fr = self._fresh(conn, {Tier.E, Tier.C}, _max_age(max_age_minutes))
            rows = [_clean({
                "date": r["date"], "weight_kg": _r((r["weight_g"] or 0) / 1000, 1) or None, "bmi": _r(r["bmi"]),
                "body_fat_pct": _r(r["body_fat_pct"]), "muscle_mass_kg": _r((r["muscle_mass_g"] or 0) / 1000) or None,
                "body_water_pct": _r(r["body_water_pct"]), "source": r["source"],
            }) for r in conn.execute("SELECT * FROM weight WHERE date BETWEEN ? AND ? ORDER BY ts_gmt LIMIT 400",
                                     (s.isoformat(), e.isoformat()))]
        out: dict[str, Any] = {"start": s.isoformat(), "end": e.isoformat(), "weigh_ins": rows}
        if not rows:
            a = load_inputs(self.app.paths).athlete
            out["note"] = "no weigh-ins from Garmin in this range"
            if a.weight_kg:
                out["manual_weight"] = {"weight_kg": a.weight_kg, "date": a.weight_date.isoformat() if a.weight_date
                                        else None, "source": "athlete.yaml"}
        return self._reply(fr, **out)

    @_safe
    def get_hr_zones(self) -> dict[str, Any]:
        with self._conn() as conn:
            fr = self._fresh(conn, {Tier.E}, None)
            zones = compute.hr_zones(conn)
        a = load_inputs(self.app.paths).athlete
        out = [_clean({
            "sport": z.get("sport"), "method": z.get("trainingMethod"),
            "zone_floors_bpm": [z.get(f"zone{i}Floor") for i in range(1, 6)], "max_hr_bpm": z.get("maxHeartRateUsed"),
            "resting_hr_bpm": z.get("restingHeartRateUsed"), "lthr_bpm": z.get("lactateThresholdHeartRateUsed"),
        }) for z in zones]
        return self._reply(fr, zones=out, athlete_overrides=_clean({"max_hr": a.max_hr, "lthr": a.lthr, "ftp": a.ftp}))

    # -- sync control ---------------------------------------------------------------------------------------------
    @_safe
    def get_sync_status(self) -> dict[str, Any]:
        with self._conn() as conn:
            return build_status(conn, self.app.settings, self.app.clock)

    @_safe
    def refresh(self, tiers: list[str] | None = None, force: bool = False, scope: str = "tiers") -> dict[str, Any]:
        if scope not in ("tiers", "history"):
            raise InputError("scope must be 'tiers' or 'history'")
        now = self.app.clock()
        jobs = self.app.jobs
        with self._refresh_lock, self._conn() as conn:
            states = repo.tier_states(conn)
            recent = [j for j in jobs_mod.list_jobs(conn, 50)
                      if j["trigger"] == "mcp" and j["queued_at"] and now - _ts(j["queued_at"]) < REFRESH_MIN_INTERVAL]
            if scope == "history":
                active = jobs.has_active(kind="backfill")
                if active:
                    return {"job_id": active["id"], "kind": "backfill", "status": active["status"],
                            "note": "a history load is already running: follow it with get_sync_status"}
                if any(j["kind"] == "backfill" for j in recent):
                    raise InputError("a history load was requested less than 5 minutes ago")
                cd = states.get(Tier.H.value, {}).get("cooldown_until")
                if cd and cd > now:
                    raise InputError(f"Garmin is in cooldown until {cd.isoformat()} (rate limit protection)")
                jid = jobs.enqueue("backfill", {Tier.H}, "mcp", force=force)
                return {"job_id": jid, "kind": "backfill", "status": "queued",
                        "note": "history load started in the background: follow it with get_sync_status"}

            try:
                wanted = {Tier(t.strip().upper()) for t in tiers} if tiers else set(REFRESH_TIERS)
            except ValueError:
                raise InputError("tiers are letters among A, B, C, D, E") from None
            if Tier.H in wanted:
                raise InputError("use scope='history' to reload the history")
            skipped: dict[str, str] = {}
            for t in sorted(wanted):
                cd = states.get(t.value, {}).get("cooldown_until")
                if cd and cd > now:
                    skipped[t.value] = f"cooldown until {cd.isoformat()}"
                elif any(t.value in j["tiers"].split(",") and j["kind"] == "refresh" for j in recent):
                    skipped[t.value] = "already refreshed on request less than 5 minutes ago"
            todo = {t for t in wanted if t.value not in skipped}
            if not todo:
                return {"job_id": None, "skipped": skipped, "note": "nothing enqueued"}
            active = jobs.has_active(todo)
            if active and jobs_mod.tiers_from_str(active["tiers"]) >= todo and not force:
                return {"job_id": active["id"], "status": active["status"], "tiers": active["tiers"],
                        "skipped": skipped, "note": "a job already refreshing these tiers is running"}
            jid = jobs.enqueue("refresh", todo, "mcp", force=force)
        return {"job_id": jid, "status": "queued", "tiers": jobs_mod.tiers_to_str(todo), "force": force,
                "skipped": skipped,
                "note": ("refetching now (ignoring the TTLs)" if force else "tiers still within their TTL are "
                         "skipped; use force=true after syncing the watch") + "; follow it with get_sync_status"}

    @_safe
    def cancel_job(self, job_id: int) -> dict[str, Any]:
        if not isinstance(job_id, int) or job_id <= 0:
            raise InputError("job_id must be a positive integer")
        with self._conn() as conn:
            res = jobs_mod.request_cancel(conn, job_id, self.app.clock)
        if res is None:
            raise InputError(f"no job #{job_id}")
        return {"job_id": job_id, "result": res}


def _ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _local_hm(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, UTC).astimezone().strftime("%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _index(desc: dict[Any, Any], key: str, default: int) -> int:
    v = desc.get(key)
    return default if v is None else int(v)


def _bb_curve(conn: sqlite3.Connection, d: date) -> list[dict[str, Any]]:
    """Body Battery levels for day ``d`` from the newest raw range payload covering it, at most one per 15 min."""
    for r in conn.execute("SELECT key, body FROM raw_payload WHERE endpoint='bodybattery.range' "
                          "ORDER BY last_seen_at DESC, id DESC"):
        try:
            a, b = parse_range_key(r["key"])
        except ValueError:
            continue
        if not a <= d <= b:
            continue
        for item in json.loads(r["body"]) or []:
            if not isinstance(item, dict) or item.get("date") != d.isoformat():
                continue
            desc = {x.get("bodyBatteryValueDescriptorKey"): x.get("bodyBatteryValueDescriptorIndex")
                    for x in item.get("bodyBatteryValueDescriptorDTOList") or [] if isinstance(x, dict)}
            ti, li = _index(desc, "timestamp", 0), _index(desc, "bodyBatteryLevel", 2)
            values = [v for v in item.get("bodyBatteryValuesArray") or [] if isinstance(v, list) and len(v) > li]
            step = max(1, -(-len(values) // CURVE_POINTS))
            return [{"time": _local_hm(v[ti]), "level": v[li]} for v in values[::step] if v[li] is not None]
    return []


def _hr_samples(conn: sqlite3.Connection, d: date) -> list[dict[str, Any]]:
    """Intraday heart rate (only fetched for today/yesterday, tier B) averaged into 10-minute buckets."""
    body = repo.latest_raw(conn, "hr.daily", d.isoformat())
    values = (body or {}).get("heartRateValues") if isinstance(body, dict) else None
    buckets: dict[int, list[int]] = {}
    for v in values or []:
        if isinstance(v, list) and len(v) >= 2 and v[0] is not None and v[1] is not None:
            buckets.setdefault(int(v[0]) // (HR_SAMPLE_MINUTES * 60_000), []).append(int(v[1]))
    return [{"time": _local_hm(k * HR_SAMPLE_MINUTES * 60_000), "bpm": round(sum(b) / len(b))}
            for k, b in sorted(buckets.items())]
