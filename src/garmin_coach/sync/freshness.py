"""The freshness gate: every consumer calls ``ensure_fresh`` before reading the cache (PLAN §6.5).

1. cheap TTL check on tier_state (one query, < 5 ms on a fresh cache)
2. cooldown check (circuit breaker)
3. refresh lock, waiting up to lock_wait_seconds, then double-check tier_state
4. stale A/B refreshed synchronously (30-call budget, 25 s wall clock)
5. stale C/D/E refreshed by one background job (or synchronously in CLI mode)
   (3-4 set the ``sync_wanted_at`` flag while waiting for the lock, so a running fit job yields)
6. never fail the consumer: errors are recorded and cached data is served flagged stale
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from ..clock import Clock, iso, system_clock
from ..garmin.endpoints import BACKGROUND_TIERS, SYNC_TIERS, Tier
from ..garmin.errors import Fetcher
from ..secrets import redact
from ..settings import Paths, Settings
from ..store import repo
from .jobs import JobManager, active_jobs
from .lock import RefreshLock
from .runner import Runner

log = logging.getLogger(__name__)

# (budget_calls, timeout_s) -> fetcher. timeout None = no wall-clock limit.
SyncFetcherFactory = Callable[[int, float | None], Fetcher]

# One background enqueue at a time per process: the MCP server builds a gate per request (one SQLite connection
# per worker thread), so the "is a job already refreshing these tiers?" check must not be per gate instance.
_BG_GUARD = threading.Lock()


class TierFreshness(BaseModel):
    age_minutes: int | None = None
    ttl_minutes: int | None = None
    stale: bool = False
    refreshed_now: bool = False
    background_refresh_started: bool = False
    stale_reason: str | None = None


class Freshness(BaseModel):
    data_through: str | None = None
    tiers: dict[str, TierFreshness] = Field(default_factory=dict)
    stale: bool = False
    stale_reason: str | None = None
    circuit_open: bool = False
    history_loaded: bool = False
    job_running: bool = False
    job: dict[str, Any] | None = None


def _age_minutes(now: datetime, ts: datetime | None) -> int | None:
    return None if ts is None else max(int((now - ts).total_seconds() // 60), 0)


class FreshnessGate:
    def __init__(
        self,
        paths: Paths,
        settings: Settings,
        conn: sqlite3.Connection,
        *,
        fetcher_factory: SyncFetcherFactory,
        jobs: JobManager | None = None,
        clock: Clock = system_clock,
        background: bool = True,
        post_run: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        """``background=False`` (CLI): C/D/E are refreshed inline because the process may exit soon."""
        self.paths = paths
        self.settings = settings
        self.conn = conn
        self.fetcher_factory = fetcher_factory
        self.jobs = jobs
        self.clock = clock
        self.background = background
        self.post_run = post_run
        self._bg_job: int | None = None

    # -- helpers --------------------------------------------------------------------------------------------------
    def _max_age(self, tier: Tier, max_age: dict[Tier, timedelta] | None) -> timedelta:
        if max_age and tier in max_age:
            return max_age[tier]
        return timedelta(minutes=self.settings.ttl_minutes(tier.value))

    def _stale(self, tiers: set[Tier], states: dict[str, dict[str, Any]], now: datetime,
               max_age: dict[Tier, timedelta] | None) -> set[Tier]:
        out = set()
        for t in tiers:
            last = states.get(t.value, {}).get("last_completed_at")
            limit = self._max_age(t, max_age)
            if last is None or limit <= timedelta(0) or now - last > limit:
                out.add(t)
        return out

    def _build(self, tiers: set[Tier], states: dict[str, dict[str, Any]], now: datetime,
               max_age: dict[Tier, timedelta] | None, stale: set[Tier], reasons: dict[Tier, str],
               refreshed: set[Tier], bg: set[Tier]) -> Freshness:
        f = Freshness()
        for t in sorted(tiers):
            st = states.get(t.value, {})
            f.tiers[t.value] = TierFreshness(
                age_minutes=_age_minutes(now, st.get("last_completed_at")),
                ttl_minutes=int(self._max_age(t, max_age).total_seconds() // 60),
                stale=t in stale,
                refreshed_now=t in refreshed,
                background_refresh_started=t in bg,
                stale_reason=reasons.get(t),
            )
            cd = st.get("cooldown_until")
            if cd and cd > now:
                f.circuit_open = True
        h = states.get(Tier.H.value, {})
        f.history_loaded = h.get("last_completed_at") is not None
        row = self.conn.execute(
            "SELECT MAX(d) AS d FROM (SELECT MAX(date) AS d FROM daily UNION ALL SELECT MAX(date) FROM sleep "
            "UNION ALL SELECT MAX(substr(start_local,1,10)) FROM activity)"
        ).fetchone()
        f.data_through = row["d"] if row else None
        running = active_jobs(self.conn)
        if running:
            j = running[0]
            f.job_running = True
            f.job = {k: j[k] for k in ("id", "kind", "status", "tiers", "calls_done", "calls_planned",
                                       "current_step")}
        # Background tiers being refreshed are "stale but being handled", not an error for the consumer.
        hard_stale = stale - bg
        f.stale = bool(hard_stale)
        if hard_stale:
            f.stale_reason = "; ".join(sorted({reasons.get(t, "stale") for t in hard_stale}))
        return f

    # -- the gate -------------------------------------------------------------------------------------------------
    def ensure_fresh(self, tiers: set[Tier] | frozenset[Tier], max_age: dict[Tier, timedelta] | None = None, *,
                     trigger: str) -> Freshness:
        tiers = set(tiers)
        now = self.clock()
        states = repo.tier_states(self.conn)
        stale = self._stale(tiers, states, now, max_age)
        reasons: dict[Tier, str] = {}
        refreshed: set[Tier] = set()
        bg: set[Tier] = set()
        if not stale:  # 1. fast path
            return self._build(tiers, states, now, max_age, stale, reasons, refreshed, bg)

        try:
            # 2. cooldowns
            for t in list(stale):
                cd = states.get(t.value, {}).get("cooldown_until")
                if cd and cd > now:
                    reasons[t] = "cooldown"
            to_refresh = {t for t in stale if t not in reasons}
            sync_part = to_refresh & SYNC_TIERS
            bg_part = to_refresh & BACKGROUND_TIERS
            if not self.background:
                sync_part |= bg_part
                bg_part = set()

            # 3-4. synchronous tiers under the lock, with double-check
            if sync_part:
                lock = RefreshLock(self.paths.lock)
                # A running fit job yields within one call when it sees this flag (PLAN-FIT §6.6).
                repo.set_flag(self.conn, repo.SYNC_WANTED, iso(self.clock()))
                try:
                    if lock.acquire(self.settings.sync.lock_wait_seconds):
                        try:
                            states = repo.tier_states(self.conn)
                            now = self.clock()
                            sync_part = self._stale(sync_part, states, now, max_age)
                            if sync_part:
                                refreshed |= self._run_sync(sync_part, max_age, reasons)
                        finally:
                            lock.release()
                    else:
                        for t in sync_part:
                            reasons[t] = "refresh in progress elsewhere (lock busy)"
                finally:
                    repo.clear_flag(self.conn, repo.SYNC_WANTED)

            # 5. background tiers
            if bg_part:
                forced = max_age is not None and all(max_age.get(t, timedelta(1)) <= timedelta(0) for t in bg_part)
                bg |= self._start_background(bg_part, trigger, reasons, force=forced)
        except Exception as exc:  # noqa: BLE001 - 6. never fail the consumer
            msg = redact(f"{type(exc).__name__}: {exc}")
            log.exception("freshness gate failed")
            repo.mark_tier_error(self.conn, (t.value for t in stale), msg, self.clock())
            for t in stale - refreshed:
                reasons.setdefault(t, msg)

        now = self.clock()
        states = repo.tier_states(self.conn)
        # A tier refreshed by this very request is fresh, even when max_age=0 ("force") makes any age stale.
        still_stale = self._stale(tiers - refreshed, states, now, max_age)
        for t in still_stale:
            if t not in reasons:
                err = states.get(t.value, {}).get("last_error")
                reasons[t] = f"refresh failed: {err}" if err else "stale"
        return self._build(tiers, states, now, max_age, still_stale, reasons, refreshed, bg)

    def _run_sync(self, tiers: set[Tier], max_age: dict[Tier, timedelta] | None,
                  reasons: dict[Tier, str]) -> set[Tier]:
        timeout = None if not self.background else self.settings.sync.sync_timeout_seconds
        budget = self.settings.sync.sync_budget_calls
        if not self.background and tiers & BACKGROUND_TIERS:
            budget = self.settings.sync.background_budget_calls
        fetcher = self.fetcher_factory(budget, timeout)
        try:
            force = max_age is not None and all(max_age.get(t, timedelta(1)) <= timedelta(0) for t in tiers)
            res = Runner(self.conn, self.settings, fetcher, clock=self.clock, on_finished=self.post_run,
                         paths=self.paths).run(tiers, force=force, max_age=max_age)
        finally:
            fetcher.close()
        for t in tiers - res.tiers_completed:
            reasons[t] = f"refresh {res.status}" + (f": {res.error}" if res.error else "")
        return res.tiers_completed

    def _start_background(self, tiers: set[Tier], trigger: str, reasons: dict[Tier, str], *,
                          force: bool = False) -> set[Tier]:
        if self.jobs is None:
            for t in tiers:
                reasons[t] = "stale (no background runner)"
            return set()
        with _BG_GUARD:
            existing = self.jobs.has_active(tiers)
            if existing:
                return tiers
            self._bg_job = self.jobs.enqueue("refresh", tiers, trigger, force=force)
        return tiers
