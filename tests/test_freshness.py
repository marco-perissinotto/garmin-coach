from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from fakes import FakeClock, FakeGarmin

from garmin_coach.garmin.client import NetworkError
from garmin_coach.garmin.endpoints import REFRESH_TIERS, Tier
from garmin_coach.settings import Paths, Settings
from garmin_coach.store import db, repo
from garmin_coach.sync.freshness import FreshnessGate
from garmin_coach.sync.jobs import JobManager, active_jobs
from garmin_coach.sync.lock import RefreshLock
from garmin_coach.sync.runner import Runner

AB = {Tier.A, Tier.B}


class Fetchers:
    """Fetcher factory recording every fake it hands out."""

    def __init__(self, make=FakeGarmin) -> None:  # type: ignore[no-untyped-def]
        self.make = make
        self.made: list[FakeGarmin] = []

    def __call__(self, budget: int, timeout: float | None = None) -> FakeGarmin:
        f = self.make()
        f.budget = budget
        self.made.append(f)
        return f

    @property
    def calls(self) -> int:
        return sum(f.calls_made for f in self.made)


def warm(conn, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    Runner(conn, settings, FakeGarmin(), clock=clock).run({Tier.H})
    Runner(conn, settings, FakeGarmin(), clock=clock).run(set(REFRESH_TIERS))


def gate(paths: Paths, settings: Settings, conn, clock: FakeClock, fetchers: Fetchers, *, jobs=None,  # type: ignore[no-untyped-def]
         background: bool = True) -> FreshnessGate:
    return FreshnessGate(paths, settings, conn, fetcher_factory=fetchers, jobs=jobs, clock=clock,
                         background=background)


def test_fresh_cache_fast_path_under_5ms(paths, settings, conn, clock) -> None:  # type: ignore[no-untyped-def]
    warm(conn, settings, clock)
    f = Fetchers()
    g = gate(paths, settings, conn, clock, f)
    g.ensure_fresh(AB, trigger="mcp")  # warm up imports/statement cache
    t0 = time.perf_counter()
    for _ in range(20):
        fr = g.ensure_fresh(AB, trigger="mcp")
    per_call_ms = (time.perf_counter() - t0) * 1000 / 20
    assert per_call_ms < 5, per_call_ms
    assert f.calls == 0 and not fr.stale and fr.history_loaded
    assert fr.tiers["A"].age_minutes == 0 and not fr.tiers["A"].refreshed_now


def test_ttl_boundary_and_sync_refresh(paths, settings, conn, clock) -> None:  # type: ignore[no-untyped-def]
    warm(conn, settings, clock)
    f = Fetchers()
    g = gate(paths, settings, conn, clock, f)
    clock.advance(minutes=60)  # exactly the TTL: still fresh
    assert g.ensure_fresh(AB, trigger="mcp").tiers["B"].refreshed_now is False
    assert f.calls == 0
    clock.advance(seconds=1)  # past the TTL: synchronous refresh
    fr = g.ensure_fresh(AB, trigger="mcp")
    assert fr.tiers["A"].refreshed_now and fr.tiers["B"].refreshed_now and not fr.stale
    assert 0 < f.calls <= 10
    assert fr.tiers["B"].age_minutes == 0


def test_max_age_zero_forces(paths, settings, conn, clock) -> None:  # type: ignore[no-untyped-def]
    warm(conn, settings, clock)
    f = Fetchers()
    fr = gate(paths, settings, conn, clock, f).ensure_fresh({Tier.B}, {Tier.B: timedelta(0)}, trigger="mcp")
    assert fr.tiers["B"].refreshed_now and f.calls == 6


def test_cooldown_suppresses_calls(paths, settings, conn, clock) -> None:  # type: ignore[no-untyped-def]
    warm(conn, settings, clock)
    clock.advance(hours=2)
    repo.set_cooldown(conn, ["A", "B"], clock() + timedelta(minutes=30), "429s")
    f = Fetchers()
    fr = gate(paths, settings, conn, clock, f).ensure_fresh(AB, trigger="mcp")
    assert f.calls == 0
    assert fr.stale and fr.circuit_open and fr.tiers["A"].stale_reason == "cooldown"


def test_client_error_served_stale_and_cooldown_set(paths, settings, conn, clock) -> None:  # type: ignore[no-untyped-def]
    warm(conn, settings, clock)
    clock.advance(hours=2)

    def broken() -> FakeGarmin:
        fg = FakeGarmin()
        fg.errors["*"] = NetworkError("connection refused")
        return fg

    f = Fetchers(broken)
    g = gate(paths, settings, conn, clock, f)
    fr = g.ensure_fresh(AB, trigger="mcp")
    assert fr.stale and "connection refused" in (fr.stale_reason or "")
    assert fr.data_through is not None  # cached data still served
    calls_after_first = f.calls
    fr2 = g.ensure_fresh(AB, trigger="mcp")  # cooldown (10 min) avoids hammering a broken connection
    assert f.calls == calls_after_first and fr2.circuit_open


def test_unexpected_exception_never_propagates(paths, settings, conn, clock) -> None:  # type: ignore[no-untyped-def]
    warm(conn, settings, clock)
    clock.advance(hours=2)

    def explode(budget: int, timeout: float | None = None) -> FakeGarmin:
        raise RuntimeError("factory exploded")

    g = FreshnessGate(paths, settings, conn, fetcher_factory=explode, clock=clock)
    fr = g.ensure_fresh(AB, trigger="mcp")
    assert fr.stale and "factory exploded" in (fr.stale_reason or "")


def test_budget_exhaustion_still_returns(paths, settings, conn, clock) -> None:  # type: ignore[no-untyped-def]
    warm(conn, settings, clock)
    clock.advance(hours=2)
    settings.sync.sync_budget_calls = 2
    f = Fetchers()
    fr = gate(paths, settings, conn, clock, f).ensure_fresh(AB, trigger="mcp")
    assert f.calls == 2 and fr.stale and "partial" in (fr.stale_reason or "")


def test_lock_contention_yields_one_refresh(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    c0 = db.connect(paths.db)
    warm(c0, settings, clock)
    clock.advance(hours=2)
    f = Fetchers()
    results = []

    def worker() -> None:
        conn = db.connect(paths.db)
        results.append(gate(paths, settings, conn, clock, f).ensure_fresh(AB, trigger="mcp"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2 and not any(r.stale for r in results)
    assert sum(1 for fg in f.made if fg.calls_made) == 1  # double-check after the lock: only one refresh
    assert sum(r.tiers["B"].refreshed_now for r in results) == 1


def test_lock_held_elsewhere_serves_stale(paths, settings, conn, clock) -> None:  # type: ignore[no-untyped-def]
    warm(conn, settings, clock)
    clock.advance(hours=2)
    settings.sync.lock_wait_seconds = 0.3
    other = RefreshLock(paths.lock)
    holder_ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        assert other.acquire(1)
        holder_ready.set()
        release.wait(5)
        other.release()

    t = threading.Thread(target=hold)
    t.start()
    holder_ready.wait(2)
    f = Fetchers()
    fr = gate(paths, settings, conn, clock, f).ensure_fresh(AB, trigger="mcp")
    release.set()
    t.join()
    assert f.calls == 0 and fr.stale and "lock busy" in (fr.stale_reason or "")


def test_background_tiers_enqueue_one_job(paths, settings, conn, clock) -> None:  # type: ignore[no-untyped-def]
    warm(conn, settings, clock)
    clock.advance(days=2)
    jf = []

    def job_fetcher(kind: str) -> FakeGarmin:
        fg = FakeGarmin()
        jf.append(fg)
        return fg

    jobs = JobManager(paths, settings, conn_factory=lambda: db.connect(paths.db), fetcher_factory=job_fetcher,
                      clock=clock)
    f = Fetchers()
    g = gate(paths, settings, conn, clock, f, jobs=jobs)
    fr = g.ensure_fresh({Tier.A, Tier.B, Tier.C, Tier.D}, trigger="mcp")
    assert fr.tiers["C"].background_refresh_started and fr.tiers["D"].background_refresh_started
    assert fr.tiers["A"].refreshed_now
    assert not fr.stale  # background tiers in progress are not reported as an error
    fr2 = g.ensure_fresh({Tier.C, Tier.D}, trigger="mcp")
    jobs_rows = conn.execute("SELECT * FROM job").fetchall()
    assert len(jobs_rows) == 1  # second request does not enqueue another job
    assert fr2.tiers["C"].background_refresh_started
    job = jobs.wait(jobs_rows[0]["id"], timeout=20)
    assert job and job["status"] == "done"
    assert active_jobs(conn) == []
    fr3 = g.ensure_fresh({Tier.C, Tier.D}, trigger="mcp")
    assert not fr3.stale and not fr3.tiers["C"].stale  # background thread updated tier_state


def test_cli_mode_refreshes_background_tiers_inline(paths, settings, conn, clock) -> None:  # type: ignore[no-untyped-def]
    warm(conn, settings, clock)
    clock.advance(days=2)
    f = Fetchers()
    fr = gate(paths, settings, conn, clock, f, background=False).ensure_fresh({Tier.C}, trigger="cli")
    assert fr.tiers["C"].refreshed_now and not fr.stale and f.calls > 0


@pytest.mark.parametrize("tiers", [AB, {Tier.C}])
def test_empty_cache_reports_history_not_loaded(paths, settings, conn, clock, tiers) -> None:  # type: ignore[no-untyped-def]
    f = Fetchers()
    fr = gate(paths, settings, conn, clock, f, background=False).ensure_fresh(tiers, trigger="mcp")
    assert fr.history_loaded is False
