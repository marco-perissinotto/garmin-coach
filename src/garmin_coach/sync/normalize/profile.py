"""HR zones, settings, fitness age, weight goal -> profile_snapshot (body kept verbatim)."""

from __future__ import annotations

import sqlite3
from typing import Any

from ...store.repo import dumps

KINDS = {
    "hrzones": "hr_zones",
    "profile.settings": "settings",
    "fitnessage.daily": "fitness_age",
    "weightgoal.range": "weight_goal",
}


def apply(conn: sqlite3.Connection, endpoint: str, body: Any, fetched_at: str) -> None:
    # Keyed by the raw row's first-fetch time, so re-seeing an identical body does not add snapshots.
    conn.execute(
        "INSERT OR REPLACE INTO profile_snapshot(taken_at, kind, body) VALUES (?,?,?)",
        (fetched_at, KINDS[endpoint], dumps(body)),
    )
