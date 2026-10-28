"""Startup check: runs once per process start and only *enqueues* (PLAN §6.6)."""

from __future__ import annotations

import logging
from datetime import timedelta

from ..clock import Clock, system_clock
from ..garmin.endpoints import REFRESH_TIERS, Tier
from ..settings import Settings
from ..store import repo
from .jobs import JobManager, active_jobs, cleanup_orphans

log = logging.getLogger(__name__)


def stale_tiers(conn, settings: Settings, clock: Clock = system_clock) -> tuple[set[Tier], set[Tier]]:  # type: ignore[no-untyped-def]
    """(stale tiers outside cooldown, stale tiers in cooldown)."""
    now = clock()
    states = repo.tier_states(conn)
    stale: set[Tier] = set()
    cooling: set[Tier] = set()
    for t in REFRESH_TIERS:
        st = states.get(t.value, {})
        last = st.get("last_completed_at")
        if last is None or now - last > timedelta(minutes=settings.ttl_minutes(t.value)):
            cd = st.get("cooldown_until")
            (cooling if cd and cd > now else stale).add(t)
    return stale, cooling


def startup_check(
    jobs: JobManager,
    settings: Settings,
    *,
    trigger: str = "startup",
    refresh_stale: bool = True,
    clock: Clock = system_clock,
) -> int | None:
    """Returns the id of the enqueued job, or None."""
    conn = jobs.conn
    cleanup_orphans(conn, settings, clock)  # 1
    if not settings.sync.startup_check:
        log.info("startup check disabled in settings")
        return None
    running = active_jobs(conn)
    if repo.is_cache_empty(conn):  # 2
        h_state = repo.tier_states(conn).get("H", {})
        cd = h_state.get("cooldown_until")
        if cd and cd > clock():
            log.info("startup: cache empty but backfill in cooldown until %s", cd)
            return None
        if any(j["kind"] == "backfill" for j in running):
            log.info("startup: backfill already running/queued (job #%s)",
                     next(j["id"] for j in running if j["kind"] == "backfill"))
            return None
        job_id = jobs.enqueue("backfill", {Tier.H}, trigger)
        log.info("startup: cache empty -> backfill job #%d", job_id)
        return job_id
    if not refresh_stale:
        return None
    stale, cooling = stale_tiers(conn, settings, clock)  # 3-4
    if cooling:
        log.info("startup: tiers in cooldown, not refreshed: %s", ",".join(sorted(t.value for t in cooling)))
    if not stale:
        log.info("startup: cache fresh, nothing to do")
        return None
    if running:
        log.info("startup: stale tiers %s but job #%s is active", sorted(t.value for t in stale), running[0]["id"])
        return None
    job_id = jobs.enqueue("refresh", stale, trigger)
    log.info("startup: stale tiers %s -> refresh job #%d", ",".join(sorted(t.value for t in stale)), job_id)
    return job_id
