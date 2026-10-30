from __future__ import annotations

import os
import socket
import threading
from datetime import timedelta

from fakes import FakeClock, FakeGarmin

from garmin_coach.clock import iso
from garmin_coach.garmin.endpoints import REFRESH_TIERS, Tier
from garmin_coach.settings import Paths, Settings
from garmin_coach.store import db, repo
from garmin_coach.sync import jobs as jobs_mod
from garmin_coach.sync.jobs import JobManager
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
