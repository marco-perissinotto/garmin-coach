"""Executes a plan: fetch -> raw_payload -> sync_state, then normalize and update tier_state (PLAN §6.2-6.4).

The runner does not take the refresh lock; callers (jobs, freshness gate) do.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..clock import Clock, system_clock
from ..garmin import endpoints as ep
from ..garmin.chunking import parse_range_key, range_key
from ..garmin.client import (
    AuthError,
    BadRequestError,
    BudgetExhaustedError,
    CircuitOpenError,
    DeadlineExceededError,
    Fetcher,
    GarminError,
    NetworkError,
    NotFoundError,
    ServerError,
)
from ..garmin.endpoints import Tier
from ..secrets import redact
from ..settings import Settings
from ..store import repo
from . import normalize
from .planner import Call, Planner

log = logging.getLogger(__name__)

# Tiers whose data a completed history backfill also refreshes (same endpoints, window ends today).
BACKFILL_ALSO_COMPLETES = (Tier.A, Tier.C, Tier.E)
# A 400 on a window this small is not a window-cap problem: the endpoint rejects the request for this account.
MIN_HALVE_DAYS = 7


@dataclass
class Progress:
    calls_planned: int
    calls_done: int
    calls_failed: int
    current_step: str


@dataclass
class RunResult:
    status: str  # done | partial | failed | cancelled
    calls_planned: int = 0
    calls_done: int = 0
    calls_failed: int = 0
    http_calls: int = 0
    tiers_completed: set[Tier] = field(default_factory=set)
    error: str | None = None
    raw_ids: list[int] = field(default_factory=list)


class CancelledError(Exception):
    pass


class Runner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        fetcher: Fetcher,
        *,
        clock: Clock = system_clock,
        progress: Callable[[Progress], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.fetcher = fetcher
        self.clock = clock
        self.progress = progress or (lambda p: None)
        self.cancel_check = cancel_check or (lambda: False)

    def run(
        self,
        tiers: set[Tier] | frozenset[Tier],
        *,
        force: bool = False,
        max_age: dict[Tier, timedelta] | None = None,
        calls: list[Call] | None = None,
    ) -> RunResult:
        now = self.clock()
        tiers = set(tiers)
        if calls is None and not force:
            tiers = {t for t in tiers if t == Tier.H or self._tier_stale(t, now, max_age)}
        planner = Planner(self.conn, self.settings, now, force=force, max_age=max_age)
        queue = deque(calls if calls is not None else planner.plan(tiers))
        res = RunResult(status="done", calls_planned=len(queue))
        failed_tiers: set[Tier] = set()
        repo.mark_tier_attempt(self.conn, (t.value for t in tiers), now)
        stop_reason: str | None = None

        def report(step: str) -> None:
            self.progress(Progress(res.calls_planned, res.calls_done, res.calls_failed, step))

        report("planned")
        while queue:
            if self.cancel_check():
                res.status, stop_reason = "cancelled", "cancelled"
                break
            call = queue.popleft()
            report(call.step)
            t0 = time.monotonic()
            status: int | None = None
            try:
                body = self.fetcher.fetch(ep.get(call.endpoint), call.args)
                status = 200 if body is not None else 204
                # Follow-ups first: activity paging compares the page with the not-yet-updated activity table.
                follow = planner.resolve(planner.expand(call, body))
                self._store(call, body, status, res)
                res.calls_done += 1
                if call.meta.get("shrunk"):
                    # Learn a window cap only once a smaller window has actually worked (PLAN §6.3).
                    repo.set_learned_cap(self.conn, call.endpoint, max(self._span(call), MIN_HALVE_DAYS),
                                         self.clock())
                if follow:
                    # Next search page first, so paging finishes before detail calls use up the budget.
                    pages = [c for c in follow if c.endpoint == "activities.search"]
                    queue.extendleft(reversed(pages))
                    queue.extend(c for c in follow if c.endpoint != "activities.search")
                    res.calls_planned += len(follow)
            except NotFoundError:
                status = 404
                repo.set_sync_state(self.conn, call.endpoint, call.key, 404, self.clock(), note="no data")
                res.calls_done += 1
            except BadRequestError as exc:
                status = 400
                halves = self._halve(call)
                if halves:
                    queue.extendleft(reversed(halves))
                    res.calls_planned += len(halves) - 1
                elif call.is_range:
                    # Even a small window is rejected: disable the endpoint for a while instead of cascading.
                    dropped = [c for c in queue if c.endpoint == call.endpoint]
                    queue = deque(c for c in queue if c.endpoint != call.endpoint)
                    ts = self.clock()
                    for c in [call, *dropped]:
                        repo.set_sync_state(self.conn, c.endpoint, c.key, 400, ts, note="endpoint rejected")
                    repo.set_endpoint_rejected(self.conn, call.endpoint, ts)
                    res.calls_planned -= len(dropped)
                    res.calls_done += 1
                    log.warning("%s rejected even a %d-day window: disabled for %d days (%s)", call.endpoint,
                                self._span(call), repo.REJECT_DAYS, exc)
                else:
                    repo.set_sync_state(self.conn, call.endpoint, call.key, 400, self.clock())
                    res.calls_failed += 1
                    failed_tiers.add(call.tier)
            except (BudgetExhaustedError, DeadlineExceededError) as exc:
                queue.appendleft(call)
                res.status, stop_reason = "partial", str(exc)
                break
            except CircuitOpenError as exc:
                status = 429
                self._cooldown(tiers, self.settings.sync.cooldown_minutes.rate_limit, exc)
                res.status, stop_reason = "failed", f"circuit open: {exc}"
                res.calls_failed += 1
                break
            except (AuthError, NetworkError) as exc:
                status = exc.status
                self._cooldown(tiers, self.settings.sync.cooldown_minutes.network_error, exc)
                res.status, stop_reason = "failed", str(exc)
                res.calls_failed += 1
                break
            except ServerError as exc:
                status = exc.status
                halves = self._halve(call)
                if halves:
                    # Seen live: dailysummaries answers 500 to some full-year windows; smaller windows work.
                    queue.extendleft(reversed(halves))
                    res.calls_planned += len(halves) - 1
                    log.warning("%s: %s, retrying with half windows", call.endpoint, exc)
                else:
                    if status is not None:
                        repo.set_sync_state(self.conn, call.endpoint, call.key, status, self.clock())
                    res.calls_failed += 1
                    failed_tiers.add(call.tier)
                    log.warning("call failed: %s", exc)
            except GarminError as exc:  # 429 after retries, other HTTP errors: skip this slice, keep going
                status = exc.status
                if status is not None:
                    repo.set_sync_state(self.conn, call.endpoint, call.key, status, self.clock())
                res.calls_failed += 1
                failed_tiers.add(call.tier)
                log.warning("call failed: %s", exc)
            finally:
                log.info(
                    "GET %s key=%s status=%s ms=%d", call.endpoint, call.key, status,
                    int((time.monotonic() - t0) * 1000),
                )

        if queue:
            failed_tiers.update(c.tier for c in queue)
        if res.status == "done" and res.calls_failed:
            res.status = "partial"
        res.http_calls = getattr(self.fetcher, "calls_made", 0)
        res.error = redact(stop_reason) if stop_reason else None

        # A tier is complete only when all its planned calls succeeded or were tombstoned (PLAN §6.2).
        if res.status in ("done", "partial"):
            done = {t for t in tiers if t not in failed_tiers}
            if Tier.H in done:
                done.update(BACKFILL_ALSO_COMPLETES)
            res.tiers_completed = done
            repo.mark_tier_complete(self.conn, (t.value for t in done), self.clock())
        if res.error:
            repo.mark_tier_error(self.conn, (t.value for t in tiers - res.tiers_completed), res.error, self.clock())
        report("finished")
        return res

    def _tier_stale(self, tier: Tier, now: datetime, max_age: dict[Tier, timedelta] | None) -> bool:
        last = repo.tier_states(self.conn).get(tier.value, {}).get("last_completed_at")
        limit = (max_age or {}).get(tier, timedelta(minutes=self.settings.ttl_minutes(tier.value)))
        return last is None or limit <= timedelta(0) or now - last > limit

    def _halve(self, call: Call) -> list[Call] | None:
        """HTTP 400 on a range call: halve the window, persist the learned cap (PLAN §6.3)."""
        if not call.is_range:
            return None
        a, b = parse_range_key(call.key)
        span = self._span(call)
        if span <= MIN_HALVE_DAYS:
            return None
        half = math.ceil(span / 2)
        mid = a + timedelta(days=half - 1)
        out = []
        for s, e in ((a, mid), (mid + timedelta(days=1), b)):
            out.append(Call(call.endpoint, range_key(s, e), {**call.args, "start": s.isoformat(), "end": e.isoformat()},
                            call.tier, f"{call.endpoint} {s}..{e} (window shrunk)", meta={"shrunk": True}))
        return out

    def _store(self, call: Call, body: object, status: int, res: RunResult) -> None:
        """raw + sync_state + normalization in one transaction: a killed process never leaves a slice marked as
        fetched but missing from the normalized tables."""
        ts = self.clock()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if body is not None:
                raw_id, _ = repo.save_raw(self.conn, call.endpoint, call.key, body, ts)
                normalize.apply_ids(self.conn, [raw_id])
                res.raw_ids.append(raw_id)
            repo.set_sync_state(self.conn, call.endpoint, call.key, status, ts)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _span(call: Call) -> int:
        a, b = parse_range_key(call.key)
        return (b - a).days + 1

    def _cooldown(self, tiers: set[Tier], minutes: int, exc: Exception) -> None:
        until: datetime = self.clock() + timedelta(minutes=minutes)
        repo.set_cooldown(self.conn, (t.value for t in tiers), until, str(exc))
