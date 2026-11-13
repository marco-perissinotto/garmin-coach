"""Build the dashboard page: payload from the cache -> one self-contained HTML file (docs/DASHBOARD.md).

The page is written to ``<app data>/dashboard/index.html``: local, per machine, like the database (it holds health
data, so it never goes to the shared profile folder). It embeds its data as JSON and needs no network to open.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Any

from .. import state as state_m
from ..athlete import load_inputs
from ..clock import Clock, local_today, system_clock
from ..context import builder
from ..settings import STATE_FILE, Paths, Settings
from ..store import repo
from . import data as data_m
from . import insights as insights_m

log = logging.getLogger(__name__)

PAGE = "index.html"
RECENT_RACE_DAYS = 14


def page_path(paths: Paths) -> Path:
    return paths.home / "dashboard" / PAGE


def payload(conn: sqlite3.Connection, paths: Paths, settings: Settings, *, clock: Clock = system_clock,
            freshness: Any | None = None) -> dict[str, Any]:
    now = clock()
    today = local_today(now)
    inputs = load_inputs(paths, today)
    builder.ensure_metrics(conn, inputs, today, builder.inputs_mtime(paths.config_dir))
    ctx = data_m.load(conn, inputs, today)
    first, by_sport = data_m.sport_ctl(ctx)
    series = data_m.daily_series(ctx, first, by_sport)
    target = data_m.target_event(ctx)
    events = data_m.events(ctx)
    state_view = state_m.view(inputs.state, [p for p in inputs.problems if p.startswith(STATE_FILE)], today,
                              settings.context.state_stale_days)
    d: dict[str, Any] = {
        "today": today.isoformat(), "today_label": insights_m.today_label(today),
        "generated": now.astimezone().strftime("%Y-%m-%d %H:%M"),
        "hero": data_m.hero(ctx), "events": events,
        "target": next((e for e in events if target and e["name"] == target.name and e["date"] == target.date.isoformat()),
                       None),
        "recent_race": any(0 <= (today - e.date).days <= RECENT_RACE_DAYS for e in inputs.events),
        "state": state_view.get("state"), "state_meta": {k: v for k, v in state_view.items() if k != "state"},
        "series": series, "ctl_history": data_m.ctl_history(ctx), "weekly": data_m.weekly(ctx),
        "intensity": data_m.intensity(ctx), "consistency": data_m.consistency(ctx),
        "run": data_m.run_block(ctx), "bike": data_m.bike_block(ctx), "swim": data_m.swim_block(ctx),
        "strength": data_m.strength(ctx), "health": data_m.health(ctx, series), "patterns": data_m.patterns(ctx),
        "event": data_m.event_compare(ctx), "years": data_m.years(ctx), "recent": data_m.recent(ctx),
        "problems": inputs.problems, "freshness": _freshness(conn, now, freshness),
    }
    d["insights"] = insights_m.build(d)
    return d


def _freshness(conn: sqlite3.Connection, now: Any, freshness: Any | None) -> dict[str, Any]:
    r = conn.execute("SELECT MAX(d) AS d FROM (SELECT MAX(date) AS d FROM daily WHERE rhr IS NOT NULL OR steps IS NOT "
                     "NULL UNION ALL SELECT MAX(date) FROM sleep UNION ALL SELECT MAX(substr(start_local,1,10)) "
                     "FROM activity)").fetchone()
    last = repo.tier_states(conn).get("A", {}).get("last_completed_at")
    minutes = None if last is None else int((now - last).total_seconds() // 60)
    out: dict[str, Any] = {"data_through": r["d"] if r else None, "activities_age_min": minutes}
    if freshness is not None and getattr(freshness, "stale", False):
        out["stale"] = freshness.stale_reason or "dati non aggiornati"
    return out


def render(d: dict[str, Any]) -> str:
    """The page with its data embedded. ``</`` is escaped so no string in the data can close the script tag."""
    template = resources.files("garmin_coach.dashboard").joinpath("page.html").read_text(encoding="utf-8")
    blob = json.dumps(d, ensure_ascii=False, separators=(",", ":"), default=str)
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return template.replace("/*__DATA__*/null", blob, 1)


def build(conn: sqlite3.Connection, paths: Paths, settings: Settings, *, clock: Clock = system_clock,
          freshness: Any | None = None) -> Path:
    target = page_path(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    html = render(payload(conn, paths, settings, clock=clock, freshness=freshness))
    tmp = target.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(target)
    return target


def after_sync(conn: sqlite3.Connection, paths: Paths, settings: Settings, clock: Clock = system_clock) -> None:
    """Post-refresh hook: rewrite the page only when it already exists (it is created on first `dashboard`).
    Never raises: a dashboard problem must not fail a sync."""
    if not settings.dashboard.auto_update or not page_path(paths).exists():
        return
    try:
        build(conn, paths, settings, clock=clock)
    except Exception:  # noqa: BLE001
        log.exception("dashboard build after sync failed")


def open_in_browser(path: Path) -> bool:
    try:
        return webbrowser.open(path.resolve().as_uri(), new=2)
    except Exception:  # noqa: BLE001 - no browser (headless machine): the caller prints the path
        log.warning("could not open a browser for %s", path)
        return False

