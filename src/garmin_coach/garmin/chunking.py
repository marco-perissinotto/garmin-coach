"""Inclusive date-range splitting that honours per-endpoint window caps."""

from __future__ import annotations

from datetime import date, timedelta

# Fixed anchor so backfill chunk boundaries (and therefore sync_state keys) are stable from one day to the
# next: resuming a backfill tomorrow finds the same chunk keys and skips what was already fetched.
ANCHOR = date(2000, 1, 3)


def split_range(start: date, end: date, cap_days: int) -> list[tuple[date, date]]:
    """Contiguous, non-overlapping inclusive chunks of at most ``cap_days`` days, starting at ``start``."""
    if cap_days < 1:
        raise ValueError("cap_days must be >= 1")
    if end < start:
        return []
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=cap_days - 1), end)
        chunks.append((cur, stop))
        cur = stop + timedelta(days=1)
    return chunks


def split_aligned(start: date, end: date, cap_days: int, anchor: date = ANCHOR) -> list[tuple[date, date]]:
    """Like :func:`split_range` but boundaries fall on ``anchor + k * cap_days``.

    The first and last chunk may be shorter than the cap; every chunk stays inside one aligned window.
    """
    if cap_days < 1:
        raise ValueError("cap_days must be >= 1")
    if end < start:
        return []
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        k = (cur - anchor).days // cap_days
        window_end = anchor + timedelta(days=(k + 1) * cap_days - 1)
        stop = min(window_end, end)
        chunks.append((cur, stop))
        cur = stop + timedelta(days=1)
    return chunks


def range_key(start: date, end: date) -> str:
    return f"{start.isoformat()}..{end.isoformat()}"


def parse_range_key(key: str) -> tuple[date, date]:
    a, b = key.split("..")
    return date.fromisoformat(a), date.fromisoformat(b)
