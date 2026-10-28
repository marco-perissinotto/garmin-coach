"""Injectable time source so TTL/cooldown/heartbeat logic is testable with a fake clock."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_today(now: datetime) -> date:
    """Garmin 'calendarDate' is the user's local day; we use this machine's local time zone."""
    return now.astimezone().date()
