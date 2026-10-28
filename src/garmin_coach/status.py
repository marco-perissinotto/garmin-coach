"""The sync status object shared by `status --json` and the MCP `get_sync_status` tool (PLAN §6.7). Never refreshes."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from .clock import Clock, parse_iso, system_clock
from .garmin.endpoints import REFRESH_TIERS, Tier
from .secrets import key_file_status
from .settings import Settings
from .store import repo
from .sync.jobs import active_jobs, list_jobs

_JOB_FIELDS = ("id", "kind", "status", "trigger", "tiers", "calls_done", "calls_planned", "calls_failed",
               "current_step", "queued_at", "started_at", "finished_at", "error")


def _eta_seconds(job: dict[str, Any], settings: Settings, clock: Clock) -> int | None:
    planned, done = job.get("calls_planned") or 0, job.get("calls_done") or 0
    remaining = max(planned - done - (job.get("calls_failed") or 0), 0)
    started = parse_iso(job.get("started_at"))
    if started and done > 0:
        per_call = (clock() - started).total_seconds() / done
    else:
        per_call = settings.sync.min_call_gap_ms / 1000 + 0.5
    return int(remaining * per_call)


def build_status(conn: sqlite3.Connection, settings: Settings, clock: Clock = system_clock) -> dict[str, Any]:
    now = clock()
    states = repo.tier_states(conn)
    tiers: dict[str, Any] = {}
    cooldowns, errors = [], []
    for t in (*REFRESH_TIERS, Tier.H):
        st = states.get(t.value, {})
        last = st.get("last_completed_at")
        ttl = None if t == Tier.H else settings.ttl_minutes(t.value)  # H = history: done once, no TTL
        cd = st.get("cooldown_until")
        tiers[t.value] = {
            "age_minutes": None if last is None else int((now - last).total_seconds() // 60),
            "ttl_minutes": ttl,
            "stale": last is None or (ttl is not None and now - last > timedelta(minutes=ttl)),
            "last_completed_at": last.isoformat() if last else None,
            "cooldown_until": cd.isoformat() if cd and cd > now else None,
            "last_error": st.get("last_error"),
        }
        if cd and cd > now:
            cooldowns.append(cd)
        if st.get("last_error"):
            errors.append((st.get("last_attempt_at") or now, st["last_error"]))
    h = states.get("H", {})
    running = active_jobs(conn)
    job = None
    if running:
        j = next((x for x in running if x["status"] == "running"), running[0])
        job = {k: j[k] for k in _JOB_FIELDS}
        job["eta_seconds"] = _eta_seconds(j, settings, clock)
    last_jobs = [
        {k: j[k] for k in ("id", "kind", "status", "trigger", "tiers", "calls_done", "calls_planned", "calls_failed",
                           "finished_at", "error")}
        for j in list_jobs(conn, 5) if j["status"] not in ("queued", "running")
    ]
    key = key_file_status()
    return {
        "history_loaded": h.get("last_completed_at") is not None,
        "history_loaded_at": h["last_completed_at"].isoformat() if h.get("last_completed_at") else None,
        "tiers": tiers,
        "job": job,
        "last_jobs": last_jobs,
        "cooldown_until": max(cooldowns).isoformat() if cooldowns else None,
        "last_error": max(errors, key=lambda e: e[0])[1] if errors else None,
        "key_age_days": key.age_days,
    }
