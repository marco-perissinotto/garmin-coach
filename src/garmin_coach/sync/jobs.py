"""Tiny in-process job runner backed by the ``job`` table (PLAN §6.7).

One daemon worker thread per process, FIFO, one job at a time. The ``job`` row is updated after every API call
so other processes (``status --watch``, the MCP server) can see progress.
"""

from __future__ import annotations

import ctypes
import logging
import os
import socket
import sqlite3
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from ..clock import Clock, iso, parse_iso, system_clock
from ..garmin.client import Fetcher, GarminError
from ..garmin.endpoints import Tier
from ..secrets import redact
from ..settings import Paths, Settings
from .lock import RefreshLock
from .runner import Progress, Runner

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
TERMINAL = ("done", "partial", "failed", "cancelled")
EXIT_CODES = {"done": 0, "partial": 2, "failed": 1, "cancelled": 1}

FetcherFactory = Callable[[str], Fetcher]  # kind -> fetcher with the right budget
ConnFactory = Callable[[], sqlite3.Connection]


def tiers_to_str(tiers: set[Tier] | frozenset[Tier] | list[Tier]) -> str:
    return ",".join(sorted(t.value for t in tiers))


def tiers_from_str(s: str) -> set[Tier]:
    return {Tier(x) for x in s.split(",") if x}


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# -- job table helpers (usable from any process) ---------------------------------------------------------------
def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def list_jobs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM job ORDER BY id DESC LIMIT ?", (limit,))]


def active_jobs(conn: sqlite3.Connection, kind: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM job WHERE status IN ('queued','running')"
    args: tuple[Any, ...] = ()
    if kind:
        q += " AND kind=?"
        args = (kind,)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]


def request_cancel(conn: sqlite3.Connection, job_id: int, clock: Clock = system_clock) -> str | None:
    job = get_job(conn, job_id)
    if job is None:
        return None
    if job["status"] == "queued":
        conn.execute(
            "UPDATE job SET status='cancelled', finished_at=?, error='cancelled before start' WHERE id=?",
            (iso(clock()), job_id),
        )
        return "cancelled"
    if job["status"] == "running":
        conn.execute("UPDATE job SET cancel_requested=1 WHERE id=?", (job_id,))
        return "cancel requested"
    return job["status"]


def prune_jobs(conn: sqlite3.Connection, keep: int = 50) -> int:
    cur = conn.execute(
        "DELETE FROM job WHERE status NOT IN ('queued','running') AND id NOT IN "
        "(SELECT id FROM job ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    return cur.rowcount


def cleanup_orphans(conn: sqlite3.Connection, settings: Settings, clock: Clock = system_clock) -> list[int]:
    """Running jobs with a stale heartbeat or a dead owner, and queued jobs of dead processes -> failed/orphaned."""
    now = clock()
    host = socket.gethostname()
    stale_after = timedelta(seconds=settings.sync.heartbeat_stale_seconds)
    orphaned: list[int] = []
    for j in active_jobs(conn):
        same_host = j["host"] == host
        dead = same_host and not pid_alive(j["pid"])
        hb = parse_iso(j["heartbeat_at"] or j["started_at"] or j["queued_at"])
        stale = j["status"] == "running" and (hb is None or now - hb > stale_after)
        if dead or stale:
            conn.execute(
                "UPDATE job SET status='failed', error='orphaned', finished_at=? WHERE id=? AND status IN ('queued','running')",
                (iso(now), j["id"]),
            )
            orphaned.append(j["id"])
    if orphaned:
        log.info("marked orphaned jobs as failed: %s", orphaned)
    return orphaned


class JobManager:
    def __init__(
        self,
        paths: Paths,
        settings: Settings,
        *,
        conn_factory: ConnFactory,
        fetcher_factory: FetcherFactory,
        clock: Clock = system_clock,
    ) -> None:
        self.paths = paths
        self.settings = settings
        self.conn_factory = conn_factory
        self.fetcher_factory = fetcher_factory
        self.clock = clock
        self.conn = conn_factory()
        self._cv = threading.Condition()
        self._worker: threading.Thread | None = None
        self._requeued: set[int] = set()

    # -- API ------------------------------------------------------------------------------------------------------
    def enqueue(self, kind: str, tiers: set[Tier] | frozenset[Tier], trigger: str, *, force: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO job(kind, trigger, tiers, status, pid, host, queued_at, force) VALUES (?,?,?,?,?,?,?,?)",
            (kind, trigger, tiers_to_str(tiers), "queued", os.getpid(), socket.gethostname(), iso(self.clock()),
             int(force)),
        )
        job_id = int(cur.lastrowid or 0)
        log.info("enqueued job #%d kind=%s tiers=%s trigger=%s force=%s", job_id, kind, tiers_to_str(tiers),
                 trigger, force)
        self._ensure_worker()
        with self._cv:
            self._cv.notify_all()
        return job_id

    def has_active(self, tiers: set[Tier] | None = None, kind: str | None = None) -> dict[str, Any] | None:
        """An active job (any process) matching ``kind`` and overlapping ``tiers``."""
        for j in active_jobs(self.conn, kind):
            if tiers is None or tiers & tiers_from_str(j["tiers"]):
                return j
        return None

    def wait(self, job_id: int, timeout: float | None = None, poll: float = 0.5) -> dict[str, Any] | None:
        waited = 0.0
        while True:
            job = get_job(self.conn, job_id)
            if job is None or job["status"] in TERMINAL:
                return job
            if timeout is not None and waited >= timeout:
                return job
            with self._cv:
                self._cv.wait(poll)
            waited += poll

    # -- worker ---------------------------------------------------------------------------------------------------
    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="garmin-coach-jobs", daemon=True)
            self._worker.start()

    def _next_job(self, conn: sqlite3.Connection) -> dict[str, Any] | None:
        r = conn.execute(
            "SELECT * FROM job WHERE status='queued' AND pid=? AND host=? ORDER BY id LIMIT 1",
            (os.getpid(), socket.gethostname()),
        ).fetchone()
        return dict(r) if r else None

    def _loop(self) -> None:
        conn = self.conn_factory()
        idle = 0.0
        while True:
            job = self._next_job(conn)
            if job is None:
                with self._cv:
                    self._cv.wait(1.0)
                idle += 1.0
                if idle > 30:  # exit when idle; restarted on next enqueue
                    return
                continue
            idle = 0.0
            try:
                self._run_job(conn, job)
            except Exception as exc:  # noqa: BLE001 - a job failure must never kill the worker
                log.exception("job #%s crashed", job["id"])
                conn.execute(
                    "UPDATE job SET status='failed', error=?, finished_at=? WHERE id=?",
                    (redact(str(exc))[:500], iso(self.clock()), job["id"]),
                )
            with self._cv:
                self._cv.notify_all()

    def _run_job(self, conn: sqlite3.Connection, job: dict[str, Any]) -> None:
        job_id = job["id"]
        lock = RefreshLock(self.paths.lock)
        if not lock.acquire(self.settings.sync.lock_wait_seconds):
            if job_id not in self._requeued:
                self._requeued.add(job_id)
                conn.execute("UPDATE job SET notes='lock busy, re-queued once' WHERE id=?", (job_id,))
                return  # still 'queued': picked again on the next loop
            conn.execute("UPDATE job SET status='failed', error='locked', finished_at=? WHERE id=?",
                         (iso(self.clock()), job_id))
            return
        try:
            row = get_job(conn, job_id)
            if row is None or row["status"] != "queued":
                return  # cancelled while waiting for the lock
            now = iso(self.clock())
            conn.execute(
                "UPDATE job SET status='running', started_at=?, heartbeat_at=? WHERE id=?", (now, now, job_id)
            )
            self._execute(conn, job)
        finally:
            lock.release()

    def _execute(self, conn: sqlite3.Connection, job: dict[str, Any]) -> None:
        job_id = job["id"]
        tiers = tiers_from_str(job["tiers"])

        def progress(p: Progress) -> None:
            conn.execute(
                "UPDATE job SET calls_planned=?, calls_done=?, calls_failed=?, current_step=?, heartbeat_at=? "
                "WHERE id=?",
                (p.calls_planned, p.calls_done, p.calls_failed, p.current_step[:200], iso(self.clock()), job_id),
            )

        def cancelled() -> bool:
            r = conn.execute("SELECT cancel_requested FROM job WHERE id=?", (job_id,)).fetchone()
            return bool(r and r["cancel_requested"])

        started = iso(self.clock())
        try:
            fetcher = self.fetcher_factory(job["kind"])
        except GarminError as exc:
            conn.execute("UPDATE job SET status='failed', error=?, finished_at=? WHERE id=?",
                         (str(exc)[:500], iso(self.clock()), job_id))
            return
        try:
            res = Runner(conn, self.settings, fetcher, clock=self.clock, progress=progress,
                         cancel_check=cancelled).run(tiers, force=bool(job["force"]))
        finally:
            fetcher.close()
        finished = iso(self.clock())
        notes = f"http_calls={res.http_calls}"
        # A backfill that stopped only because of its call budget continues in a new job (same trigger), so a
        # long history loads in one go instead of waiting for the next process start (PLAN §6.2, §6.7).
        # Enqueued *before* this job turns terminal, so whoever sees 'partial' also sees the continuation.
        if (job["kind"] == "backfill" and res.status == "partial" and res.calls_done > 0 and res.error
                and "budget" in res.error and not cancelled()):
            nxt = self.enqueue("backfill", tiers, job["trigger"], force=bool(job["force"]))
            notes += f" -> continued by #{nxt}"
        conn.execute(
            "UPDATE job SET status=?, finished_at=?, heartbeat_at=?, error=?, calls_planned=?, calls_done=?, "
            "calls_failed=?, notes=? WHERE id=?",
            (res.status, finished, finished, res.error, res.calls_planned, res.calls_done, res.calls_failed,
             notes, job_id),
        )
        conn.execute(
            "INSERT INTO sync_run(job_id, started_at, finished_at, trigger, tiers, calls, errors, notes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, started, finished, job["trigger"], job["tiers"], res.http_calls, res.calls_failed, res.error),
        )
        log.info("job #%d %s: %d/%d calls, %d failed, %d HTTP", job_id, res.status, res.calls_done,
                 res.calls_planned, res.calls_failed, res.http_calls)
