"""Composition root: wires paths, settings, DB, Garmin client factories, job manager and freshness gate."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from .clock import Clock, system_clock
from .garmin.client import Fetcher, GarminClient
from .settings import Paths, Settings, get_paths, load_settings
from .store import db
from .sync.freshness import FreshnessGate
from .sync.jobs import JobManager


@dataclass
class App:
    paths: Paths
    settings: Settings
    clock: Clock = system_clock
    _conn: sqlite3.Connection | None = field(default=None, repr=False)
    _jobs: JobManager | None = field(default=None, repr=False)

    @classmethod
    def load(cls) -> App:
        paths = get_paths().ensure()
        return cls(paths=paths, settings=load_settings(paths))

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.paths.db)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self.connect()
        return self._conn

    def fetcher(self, budget: int, timeout_s: float | None = None, gap_ms: int | None = None) -> Fetcher:
        deadline = time.monotonic() + timeout_s if timeout_s else None
        gap = self.settings.sync.min_call_gap_ms if gap_ms is None else gap_ms
        return GarminClient(budget=budget, min_gap_s=gap / 1000, deadline=deadline)

    def job_fetcher(self, kind: str) -> Fetcher:
        s = self.settings.sync
        if kind == "backfill":
            return self.fetcher(s.backfill_budget_calls, gap_ms=max(s.backfill_call_gap_ms, s.min_call_gap_ms))
        return self.fetcher(s.background_budget_calls)

    @property
    def jobs(self) -> JobManager:
        if self._jobs is None:
            self._jobs = JobManager(self.paths, self.settings, conn_factory=self.connect,
                                    fetcher_factory=self.job_fetcher, clock=self.clock)
        return self._jobs

    def gate(self, *, background: bool) -> FreshnessGate:
        return FreshnessGate(self.paths, self.settings, self.conn, fetcher_factory=self.fetcher, jobs=self.jobs,
                             clock=self.clock, background=background)
