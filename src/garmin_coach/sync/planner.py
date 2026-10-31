"""Decides which (endpoint, key) calls are due for the requested tiers (PLAN §6.2).

The plan is computed before executing. Activity discovery is the one dynamic part: after a search page is
fetched, :meth:`Planner.expand` adds the next page and per-activity detail calls, and the runner bumps
``calls_planned`` accordingly.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ..clock import local_today, parse_iso
from ..fit import store as fit_store
from ..garmin import endpoints as ep
from ..garmin.chunking import range_key, split_aligned, split_range
from ..garmin.endpoints import Tier
from ..settings import Settings
from ..store import repo
from .normalize.activities import summary_hash

TOMBSTONE_TTL = timedelta(hours=24)
OK_STATUSES = (200, 204, 404)
FETCHED_ONCE = ("activity.detail", "activity.workouts", "activity.download")
RECENT_PAGE_LIMIT = 20
RECENT_MAX_PAGES = 3
BACKFILL_PAGE_LIMIT = 100
WEIGHT_GOAL_DAYS = 30
MIN_LEARNED_CAP_DAYS = 7


@dataclass
class Call:
    endpoint: str
    key: str
    args: dict[str, Any]
    tier: Tier
    step: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_range(self) -> bool:
        return "start" in self.args and "end" in self.args


class Planner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        now: datetime,
        *,
        force: bool = False,
        max_age: dict[Tier, timedelta] | None = None,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.now = now
        self.today = local_today(now)
        self.force = force
        self.max_age = max_age or {}
        self._seen: set[tuple[str, str]] = set()

    # -- policy -----------------------------------------------------------------------------------------------
    def ttl(self, tier: Tier) -> timedelta:
        if tier in self.max_age:
            return self.max_age[tier]
        if tier == Tier.H:
            return timedelta.max
        return timedelta(minutes=self.settings.ttl_minutes(tier.value))

    def cap(self, endpoint: str) -> int | None:
        default = ep.get(endpoint).cap_days
        learned = repo.learned_cap(self.conn, endpoint) or None  # 0 = rejected, handled by enabled()
        if learned is not None:
            learned = max(learned, MIN_LEARNED_CAP_DAYS)  # never plan one-day windows from a stale learned cap
        if default is None:
            return learned
        return min(default, learned) if learned else default

    def enabled(self, endpoint: str) -> bool:
        flag = ep.get(endpoint).optional_flag
        if flag is not None and not getattr(self.settings.optional_endpoints, flag, False):
            return False
        return not repo.endpoint_rejected(self.conn, endpoint, self.now)

    def _is_past_day_key(self, key: str) -> bool:
        last = key.split("..")[-1]
        try:
            return date.fromisoformat(last) < self.today - timedelta(days=1)
        except ValueError:
            return False

    def is_fresh(self, call: Call) -> bool:
        """True when this slice was fetched recently enough to skip (PLAN §6.2, §6.3 tombstones)."""
        st = repo.get_sync_state(self.conn, call.endpoint, call.key)
        if st is None:
            return False
        if call.endpoint == "activity.download" and fit_store.attempted(self.conn, int(call.key)):
            return True  # tried once (a 400 too, PLAN-FIT §6.4); 429/5xx are retried
        if st["last_status"] not in OK_STATUSES:
            return False
        if call.endpoint in FETCHED_ONCE:
            return True  # immutable enough: fetched once, refreshed only on explicit request
        if self.force and call.tier != Tier.H:
            return False
        fetched = parse_iso(st["last_fetched_at"])
        assert fetched is not None
        age = self.now - fetched
        # 404 = no data. For past days, don't retry for 24 h; today/yesterday follow the tier TTL (data may arrive).
        if st["last_status"] == 404 and self._is_past_day_key(call.key):
            return age < TOMBSTONE_TTL
        if call.tier == Tier.H:
            return st["last_status"] != 404 or age < TOMBSTONE_TTL
        return age < self.ttl(call.tier)

    # -- building ---------------------------------------------------------------------------------------------
    def _add(self, out: list[Call], call: Call) -> None:
        ident = (call.endpoint, call.key)
        if ident in self._seen or not self.enabled(call.endpoint):
            return
        self._seen.add(ident)
        if not self.is_fresh(call):
            out.append(call)
        elif call.endpoint == "activities.search":
            # Already fetched (e.g. a resumed backfill): continue discovery from the stored page.
            body = repo.latest_raw(self.conn, call.endpoint, call.key)
            for follow in self.expand(call, body):
                self._add(out, follow)

    def _range(self, out: list[Call], endpoint: str, start: date, end: date, tier: Tier, aligned: bool) -> None:
        cap = self.cap(endpoint) or (end - start).days + 1
        chunks = (split_aligned if aligned else split_range)(start, end, cap)
        for i, (a, b) in enumerate(chunks, 1):
            label = f"{endpoint} {a}..{b}" + (f" ({i}/{len(chunks)})" if len(chunks) > 1 else "")
            self._add(out, Call(endpoint, range_key(a, b), {"start": a.isoformat(), "end": b.isoformat()}, tier, label))

    def _day(self, out: list[Call], endpoint: str, d: date, tier: Tier) -> None:
        self._add(out, Call(endpoint, d.isoformat(), {"date": d.isoformat()}, tier, f"{endpoint} {d}"))

    def _static(self, out: list[Call], endpoint: str, tier: Tier) -> None:
        self._add(out, Call(endpoint, "-", {}, tier, endpoint))

    def _search(self, start: int, limit: int, tier: Tier, start_date: date | None = None) -> Call:
        args: dict[str, Any] = {"start": start, "limit": limit}
        if start_date is not None:
            args["start_date"] = start_date.isoformat()
            args["end_date"] = self.today.isoformat()
            key = f"backfill:{start_date}:{start}:{limit}"
        else:
            key = f"recent:{start}:{limit}"
        return Call("activities.search", key, args, tier, f"activity list page {start // limit + 1}")

    def plan(self, tiers: set[Tier] | frozenset[Tier]) -> list[Call]:
        out: list[Call] = []
        t, y = self.today, self.today - timedelta(days=1)
        for tier in sorted(tiers, key=lambda x: "ABHCDEF".index(x.value)):
            if tier == Tier.A:
                self._add(out, self._search(0, RECENT_PAGE_LIMIT, Tier.A))
            elif tier == Tier.B:
                self._day(out, "usersummary.daily", t, Tier.B)
                self._day(out, "usersummary.daily", y, Tier.B)
                self._day(out, "sleep.daily", t, Tier.B)
                self._day(out, "hrv.daily", t, Tier.B)
                self._day(out, "hr.daily", t, Tier.B)
                self._range(out, "bodybattery.range", y, t, Tier.B, aligned=False)
            elif tier == Tier.C:
                for name in ep.TIER_ENDPOINTS[Tier.C]:
                    self._range(out, name, t - timedelta(days=6), t, Tier.C, aligned=False)
            elif tier == Tier.D:
                for name in ep.TIER_ENDPOINTS[Tier.D]:
                    self._day(out, name, t, Tier.D)
            elif tier == Tier.E:
                self._e_calls(out, Tier.E)
            elif tier == Tier.H:
                start = t - timedelta(days=self.settings.sync.backfill_days)
                self._add(out, self._search(0, BACKFILL_PAGE_LIMIT, Tier.H, start_date=start))
                for name in ep.TIER_ENDPOINTS[Tier.H]:
                    if ep.get(name).kind == "range" and name != "weightgoal.range":
                        self._range(out, name, start, t, Tier.H, aligned=True)
                self._e_calls(out, Tier.H)
            elif tier == Tier.F:
                # The whole eligible history, newest first: the job's call budget bounds it and the chain continues
                # (PLAN-FIT D-073).
                for aid in fit_store.pending_ids(self.conn, self.settings, t):
                    self._add(out, self._download(aid, Tier.F))
        return out

    def _download(self, activity_id: int, tier: Tier, start_local: str = "") -> Call:
        if not start_local:
            row = self.conn.execute("SELECT start_local FROM activity WHERE activity_id=?", (activity_id,)).fetchone()
            start_local = str((row["start_local"] if row else None) or "")[:10]
        return Call("activity.download", str(activity_id), {"activity_id": activity_id}, tier,
                    f"activity.download {activity_id} ({start_local})")

    def wants_fit(self, type_key: str | None) -> bool:
        return self.settings.fit.download and not fit_store.skipped_type(self.settings, type_key)

    def _e_calls(self, out: list[Call], tier: Tier) -> None:
        t = self.today
        self._range(out, "weightgoal.range", t - timedelta(days=WEIGHT_GOAL_DAYS), t, tier, aligned=False)
        self._static(out, "hrzones", tier)
        self._day(out, "fitnessage.daily", t, tier)
        self._static(out, "profile.settings", tier)

    # -- dynamic expansion --------------------------------------------------------------------------------------
    def expand(self, call: Call, body: Any) -> list[Call]:
        """Follow-up calls after an activity search page: next page + detail/workouts (and the FIT download, tier F)
        for recent new activities."""
        if call.endpoint != "activities.search" or not isinstance(body, list):
            return []
        out: list[Call] = []
        limit = int(call.args.get("limit", RECENT_PAGE_LIMIT))
        start = int(call.args.get("start", 0))
        detail_cutoff = (self.today - timedelta(days=self.settings.sync.detail_days)).isoformat()
        all_new = True
        for a in body:
            if not isinstance(a, dict) or a.get("activityId") is None:
                continue
            aid = int(a["activityId"])
            known = self.conn.execute(
                "SELECT summary_hash, has_detail FROM activity WHERE activity_id=?", (aid,)
            ).fetchone()
            if known is not None and known["summary_hash"] == summary_hash(a):
                all_new = False
            start_local = str(a.get("startTimeLocal") or "")[:10]
            if start_local >= detail_cutoff and not (known and known["has_detail"]):
                for name in ("activity.detail", "activity.workouts"):
                    out.append(Call(name, str(aid), {"activity_id": aid}, call.tier, f"{name} {aid} ({start_local})"))
            if start_local >= detail_cutoff and self.wants_fit(dig_type_key(a)):
                # Tier F: a failed download never leaves tier A or H incomplete (PLAN-FIT D-071).
                out.append(self._download(aid, Tier.F, start_local))
        full_page = len(body) >= limit
        if call.tier == Tier.H:
            more = full_page  # backfill pages through the whole window
        else:
            # Newest first: once a page holds a known, unchanged activity, everything older is known too.
            more = full_page and all_new and (start // limit + 1) < RECENT_MAX_PAGES
        if more:
            nxt = Call(
                "activities.search",
                call.key.rsplit(":", 2)[0] + f":{start + limit}:{limit}",
                {**call.args, "start": start + limit},
                call.tier,
                f"activity list page {start // limit + 2}",
            )
            out.insert(0, nxt)
        return out

    def resolve(self, calls: list[Call]) -> list[Call]:
        """Filter follow-up calls through the same freshness/dedupe rules as the initial plan."""
        out: list[Call] = []
        for c in calls:
            self._add(out, c)
        return out

    def lazy_activity_calls(self, activity_id: int, *, want_fit: bool) -> list[Call]:
        """``get_activity`` on one activity: detail + workouts when missing, plus the FIT download when ``want_fit``
        (the caller checks ``fit.download`` and the type) and not tried yet. Fresh calls are filtered out."""
        calls = [Call(n, str(activity_id), {"activity_id": activity_id}, Tier.A, f"{n} {activity_id}")
                 for n in ("activity.detail", "activity.workouts")]
        if want_fit:
            calls.append(self._download(activity_id, Tier.F))
        return self.resolve(calls)


def dig_type_key(a: dict[str, Any]) -> str | None:
    t = a.get("activityType")
    return t.get("typeKey") if isinstance(t, dict) else None
