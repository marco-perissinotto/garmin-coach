from __future__ import annotations

import os
import socket
import threading
from datetime import timedelta

from fakes import FakeClock, FakeGarmin

from garmin_coach.clock import iso
from garmin_coach.fit import store as fit_store
from garmin_coach.garmin.endpoints import REFRESH_TIERS, Tier
from garmin_coach.settings import Paths, Settings
from garmin_coach.store import db, repo
from garmin_coach.sync import jobs as jobs_mod
from garmin_coach.sync.freshness import FreshnessGate
from garmin_coach.sync.jobs import JobManager
from garmin_coach.sync.lock import RefreshLock
from garmin_coach.sync.runner import Runner
from garmin_coach.sync.startup import startup_check


class JobFetchers:
    def __init__(self, budget: int = 10_000, slow: float = 0.0) -> None:
        self.budget = budget
        self.slow = slow
        self.made: list[FakeGarmin] = []

    def __call__(self, kind: str) -> FakeGarmin:
        f = FakeGarmin(budget=self.budget)
        if self.slow:
            import time

            f.errors["*"] = lambda args: time.sleep(self.slow)
        self.made.append(f)
        return f

    @property
    def log(self) -> list[tuple[str, dict]]:
        return [x for f in self.made for x in f.log]


def manager(paths: Paths, settings: Settings, clock: FakeClock, fetchers: JobFetchers) -> JobManager:
    return JobManager(paths, settings, conn_factory=lambda: db.connect(paths.db), fetcher_factory=fetchers,
                      clock=clock)


def test_empty_db_enqueues_exactly_one_backfill(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    f = JobFetchers(slow=0.002)
    m = manager(paths, settings, clock, f)
    jid = startup_check(m, settings, clock=clock)
    assert jid is not None and jobs_mod.get_job(m.conn, jid)["kind"] == "backfill"
    # a second process start while it is queued/running enqueues nothing
    m2 = manager(paths, settings, clock, JobFetchers())
    assert startup_check(m2, settings, clock=clock) is None
    job = m.wait(jid, timeout=60)
    assert job["status"] == "done" and 0 < job["calls_done"] <= 200
    assert not repo.is_cache_empty(m.conn)


def test_progress_is_monotonic_and_visible(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    f = JobFetchers(slow=0.003)
    m = manager(paths, settings, clock, f)
    jid = m.enqueue("backfill", {Tier.H}, "cli")
    observer = db.connect(paths.db)  # another connection, like `status --watch` in a second terminal
    seen: list[int] = []
    while True:
        j = jobs_mod.get_job(observer, jid)
        seen.append(j["calls_done"] or 0)
        if j["status"] in jobs_mod.TERMINAL:
            break
        threading.Event().wait(0.02)
    assert seen == sorted(seen) and seen[-1] > 0 and len(set(seen)) > 2
    assert j["status"] == "done" and j["heartbeat_at"] and j["current_step"] == "finished"


def test_partial_backfill_resumes_without_refetch(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    settings.sync.backfill_budget_calls = 50
    release = threading.Event()

    class GatedFetchers(JobFetchers):
        """The continuation job's first call waits until the test has cancelled it: the worker may pick the job up
        before the test reaches it (seen on fast Linux runners), so it can be either queued or running."""

        def __call__(self, kind: str) -> FakeGarmin:
            f = super().__call__(kind)
            if len(self.made) > 1:
                f.errors["*"] = lambda args: release.wait(10)
            return f

    f1 = GatedFetchers(budget=50)
    m = manager(paths, settings, clock, f1)
    first = m.wait(startup_check(m, settings, clock=clock), timeout=60)  # type: ignore[arg-type]
    assert first["status"] == "partial"
    # A continuation job was enqueued; simulate the process dying before it completes (cancel it), then a new
    # process start must resume the partial history without refetching.
    cont = m.has_active(kind="backfill")
    assert cont is not None
    assert jobs_mod.request_cancel(m.conn, cont["id"]) in ("cancelled", "cancel requested")
    release.set()
    assert m.wait(cont["id"], timeout=60)["status"] == "cancelled"  # type: ignore[index]
    f2 = JobFetchers()
    m2 = manager(paths, settings, clock, f2)
    jid = startup_check(m2, settings, clock=clock)  # partial backfill -> new backfill job (resume)
    assert jid is not None and jobs_mod.get_job(m2.conn, jid)["kind"] == "backfill"
    assert m2.wait(jid, timeout=60)["status"] == "done"
    key = lambda x: (x[0], tuple(sorted(x[1].items())))  # noqa: E731
    assert {key(x) for x in f1.log} & {key(x) for x in f2.log} == set()


def test_orphaned_running_job_is_failed_and_resumed(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    conn = db.connect(paths.db)
    # simulate a killed process mid-backfill: some slices fetched, job row left 'running'
    killed = FakeGarmin(budget=40)
    Runner(conn, settings, killed, clock=clock).run({Tier.H})
    conn.execute(
        "INSERT INTO job(kind, trigger, tiers, status, pid, host, queued_at, started_at, heartbeat_at) "
        "VALUES ('backfill','startup','H','running',?,?,?,?,?)",
        (999_999_999, socket.gethostname(), iso(clock()), iso(clock()), iso(clock())),
    )
    f = JobFetchers()
    m = manager(paths, settings, clock, f)
    jid = startup_check(m, settings, clock=clock)
    old = conn.execute("SELECT status, error FROM job WHERE pid=999999999").fetchone()
    assert old["status"] == "failed" and old["error"] == "orphaned"
    assert m.wait(jid, timeout=60)["status"] == "done"  # type: ignore[arg-type]
    key = lambda x: (x[0], tuple(sorted(x[1].items())))  # noqa: E731
    assert {key(x) for x in killed.log} & {key(x) for x in f.log} == set()


def test_stale_heartbeat_is_orphaned_even_if_pid_alive(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    conn = db.connect(paths.db)
    old = clock() - timedelta(minutes=10)
    conn.execute(
        "INSERT INTO job(kind, trigger, tiers, status, pid, host, queued_at, started_at, heartbeat_at) "
        "VALUES ('refresh','startup','A,B','running',?,?,?,?,?)",
        (os.getpid(), socket.gethostname(), iso(old), iso(old), iso(old)),
    )
    assert jobs_mod.cleanup_orphans(conn, settings, clock) != []


def test_stale_cache_enqueues_refresh_and_cooldown_enqueues_nothing(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    conn = db.connect(paths.db)
    Runner(conn, settings, FakeGarmin(), clock=clock).run({Tier.H})
    Runner(conn, settings, FakeGarmin(), clock=clock).run(set(REFRESH_TIERS))
    f = JobFetchers()
    m = manager(paths, settings, clock, f)
    assert startup_check(m, settings, clock=clock) is None  # all fresh
    clock.advance(hours=2)
    repo.set_cooldown(conn, [t.value for t in REFRESH_TIERS], clock() + timedelta(hours=1), "429")
    assert startup_check(m, settings, clock=clock) is None  # cooldown -> nothing
    clock.advance(hours=1, minutes=1)
    jid = startup_check(m, settings, clock=clock)
    job = jobs_mod.get_job(conn, jid)  # type: ignore[arg-type]
    assert job["kind"] == "refresh" and set(job["tiers"].split(",")) == {"A", "B"}
    assert m.wait(jid, timeout=30)["status"] == "done"  # type: ignore[arg-type]


def test_startup_disabled(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    settings.sync.startup_check = False
    assert startup_check(manager(paths, settings, clock, JobFetchers()), settings, clock=clock) is None


def test_cancel_stops_between_calls(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    f = JobFetchers(slow=0.02)
    m = manager(paths, settings, clock, f)
    jid = m.enqueue("backfill", {Tier.H}, "cli")
    while (jobs_mod.get_job(m.conn, jid)["calls_done"] or 0) < 3:
        threading.Event().wait(0.01)
    assert jobs_mod.request_cancel(m.conn, jid) == "cancel requested"
    job = m.wait(jid, timeout=30)
    assert job["status"] == "cancelled" and job["calls_done"] < 50
    assert repo.is_cache_empty(m.conn)


def test_cancel_queued_job(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    conn = db.connect(paths.db)
    conn.execute("INSERT INTO job(kind, trigger, tiers, status, pid, host) VALUES ('refresh','cli','A','queued',1,'x')")
    assert jobs_mod.request_cancel(conn, 1) == "cancelled"


def test_prune_keeps_last_n(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    conn = db.connect(paths.db)
    for _ in range(60):
        conn.execute("INSERT INTO job(kind, trigger, tiers, status) VALUES ('refresh','cli','A','done')")
    assert jobs_mod.prune_jobs(conn, 50) == 10


def test_budget_limited_backfill_chains_until_done(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    """A long history (>400 calls) loads in one go: each budget-limited job enqueues the next."""
    settings.sync.backfill_budget_calls = 50
    f = JobFetchers(budget=50)
    m = manager(paths, settings, clock, f)
    first = startup_check(m, settings, clock=clock)
    assert first is not None
    last = m.wait(first, timeout=60)
    chain = [last]
    while last["status"] == "partial":
        nxt = m.has_active(kind="backfill")
        assert nxt is not None, "partial backfill was not continued"
        last = m.wait(nxt["id"], timeout=60)
        chain.append(last)
    assert last["status"] == "done" and len(chain) >= 3
    assert not repo.is_cache_empty(m.conn)
    assert "continued by" in (chain[0]["notes"] or "")
    key = lambda x: (x[0], tuple(sorted(x[1].items())))  # noqa: E731
    seen = [key(x) for fg in f.made for x in fg.log]
    assert len(seen) == len(set(seen))  # chained jobs never refetch a slice
    assert sum(fg.calls_made for fg in f.made) <= 200


# -- fit jobs (PLAN-FIT §6.5-6.6) -------------------------------------------------------------------------------------
def warm_without_fit(paths: Paths, settings: Settings, clock: FakeClock) -> int:
    """History loaded with FIT downloads off; returns the number of activities a fit backfill must try."""
    conn = db.connect(paths.db)
    settings.fit.download = False
    Runner(conn, settings, FakeGarmin(), clock=clock).run({Tier.H})
    Runner(conn, settings, FakeGarmin(), clock=clock).run(set(REFRESH_TIERS))
    settings.fit.download = True
    settings.fit.backfill_days = 60  # ~25 activities keep the test fast
    n = len(fit_store.pending_ids(conn, settings, clock().astimezone().date()))
    conn.close()
    return n


def follow_chain(m: JobManager, first: int) -> list[dict]:
    chain = [m.wait(first, timeout=60)]
    while chain[-1]["status"] == "partial":
        nxt = m.has_active(kind="fit")
        assert nxt is not None, "partial fit job was not continued"
        chain.append(m.wait(nxt["id"], timeout=60))
    return chain


def test_fit_jobs_chain_on_budget_until_every_activity_is_tried(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    n = warm_without_fit(paths, settings, clock)
    f = JobFetchers(budget=10)
    m = manager(paths, settings, clock, f)
    chain = follow_chain(m, m.enqueue("fit", {Tier.F}, "cli"))
    assert chain[-1]["status"] == "done" and len(chain) == n // 10 + 1
    assert "continued by" in (chain[0]["notes"] or "") and "budget" in (chain[0]["error"] or "")
    ids = [a["activity_id"] for name, a in f.log if name == "activity.download"]
    assert len(ids) == len(set(ids)) == n
    assert m.conn.execute("SELECT COUNT(*) FROM activity_fit WHERE status='ok'").fetchone()[0] == n
    assert len(list(paths.fit_dir.glob("*.zip"))) == n  # the job keeps the files


def test_fit_job_yields_to_an_on_demand_refresh_and_continues(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    n = warm_without_fit(paths, settings, clock)
    flag_conn = db.connect(paths.db)

    def want_sync_at_call_5(args) -> None:  # type: ignore[no-untyped-def]
        if f.made[0].calls_made == 5 and not repo.get_flag(flag_conn, repo.SYNC_WANTED):
            repo.set_flag(flag_conn, repo.SYNC_WANTED, iso(clock()))

    class Hooked(JobFetchers):
        def __call__(self, kind: str) -> FakeGarmin:
            fg = super().__call__(kind)
            if len(self.made) == 1:
                fg.errors["*"] = want_sync_at_call_5
            return fg

    f = Hooked()
    m = manager(paths, settings, clock, f)
    first = m.wait(m.enqueue("fit", {Tier.F}, "mcp"), timeout=60)
    assert first["status"] == "partial" and first["error"] == "yielded: on-demand refresh waiting"
    assert first["calls_done"] == 5 and "continued by" in first["notes"]
    cont = m.has_active(kind="fit")
    assert cont is not None and cont["trigger"] == "mcp"
    repo.clear_flag(flag_conn, repo.SYNC_WANTED)  # the refresh is over
    chain = follow_chain(m, cont["id"])
    assert chain[-1]["status"] == "done"
    assert sum(fg.counts()["activity.download"] for fg in f.made) == n
    flag_conn.close()


def test_fit_continuation_retries_the_lock(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    warm_without_fit(paths, settings, clock)
    settings.sync.lock_wait_seconds = 0.2
    holder = RefreshLock(paths.lock)
    assert holder.acquire(1)
    m = manager(paths, settings, clock, JobFetchers())
    jid = m.enqueue("fit", {Tier.F}, "cli")
    threading.Event().wait(1.0)  # ~4 failed attempts of 0.2 s: a non-fit job would have failed 'locked'
    holder.release()
    job = m.wait(jid, timeout=60)
    assert job["status"] == "done" and job["error"] is None  # not failed 'locked'


def test_cancel_stops_the_fit_chain(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    warm_without_fit(paths, settings, clock)
    m = manager(paths, settings, clock, JobFetchers(slow=0.05))
    jid = m.enqueue("fit", {Tier.F}, "cli")
    while (jobs_mod.get_job(m.conn, jid)["calls_done"] or 0) < 2:
        threading.Event().wait(0.01)
    jobs_mod.request_cancel(m.conn, jid)
    assert m.wait(jid, timeout=30)["status"] == "cancelled"  # type: ignore[index]
    assert m.has_active(kind="fit") is None


def test_startup_never_enqueues_a_fit_job(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    n = warm_without_fit(paths, settings, clock)
    assert n > 0
    m = manager(paths, settings, clock, JobFetchers())
    clock.advance(hours=2)
    jid = startup_check(m, settings, clock=clock)
    assert jid is not None and jobs_mod.get_job(m.conn, jid)["kind"] == "refresh"  # type: ignore[index]
    m.wait(jid, timeout=30)
    assert {j["kind"] for j in jobs_mod.list_jobs(m.conn)} == {"refresh"}


def test_sync_wanted_flag_set_by_the_gate_and_stale_after_30_s(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    warm_without_fit(paths, settings, clock)
    conn = db.connect(paths.db)
    seen: list[str | None] = []

    def fetchers(budget: int, timeout: float | None = None) -> FakeGarmin:
        fg = FakeGarmin(budget=budget)
        fg.errors["*"] = lambda args: seen.append(repo.get_flag(conn, repo.SYNC_WANTED))
        return fg

    clock.advance(hours=2)
    fr = FreshnessGate(paths, settings, conn, fetcher_factory=fetchers, clock=clock).ensure_fresh({Tier.A, Tier.B},
                                                                                                  trigger="mcp")
    assert fr.tiers["A"].refreshed_now and seen and all(v == iso(clock()) for v in seen)
    assert repo.get_flag(conn, repo.SYNC_WANTED) is None  # cleared after the lock is released
    # A flag left behind by a crashed process is ignored after 30 s: the fit job neither waits nor yields
    repo.set_flag(conn, repo.SYNC_WANTED, iso(clock() - timedelta(seconds=31)))
    assert not repo.sync_wanted(conn, clock())
    m = manager(paths, settings, clock, JobFetchers())
    job = m.wait(m.enqueue("fit", {Tier.F}, "cli"), timeout=30)
    assert job["status"] == "done" and job["error"] is None  # type: ignore[index]
    conn.close()


def test_refresh_job_waiting_for_the_lock_asks_a_fit_chain_to_yield(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    warm_without_fit(paths, settings, clock)
    settings.sync.lock_wait_seconds = 1
    conn = db.connect(paths.db)
    holder = RefreshLock(paths.lock)  # stands in for a fit job of another process
    assert holder.acquire(1)
    m = manager(paths, settings, clock, JobFetchers())
    jid = m.enqueue("refresh", {Tier.C}, "gate", force=True)
    flags = []
    for _ in range(40):
        flags.append(repo.get_flag(conn, repo.SYNC_WANTED))
        if flags[-1]:
            break
        threading.Event().wait(0.02)
    holder.release()
    assert any(flags)
    assert m.wait(jid, timeout=30)["status"] == "done"  # type: ignore[index]
    assert repo.get_flag(conn, repo.SYNC_WANTED) is None
    conn.close()
