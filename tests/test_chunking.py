from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from garmin_coach.garmin.chunking import parse_range_key, range_key, split_aligned, split_range


def _check(chunks: list[tuple[date, date]], start: date, end: date, cap: int) -> None:
    assert chunks[0][0] == start and chunks[-1][1] == end  # union == original range
    for a, b in chunks:
        assert a <= b and (b - a).days + 1 <= cap  # every chunk <= cap, inclusive dates
    for (_, b), (c, _) in zip(chunks, chunks[1:], strict=False):
        assert c == b + timedelta(days=1)  # contiguous, non-overlapping
    assert sum((b - a).days + 1 for a, b in chunks) == (end - start).days + 1


@pytest.mark.parametrize("cap", [1, 7, 28, 31, 365])
@pytest.mark.parametrize("fn", [split_range, split_aligned])
def test_chunk_properties_random(fn, cap: int) -> None:  # type: ignore[no-untyped-def]
    rnd = random.Random(cap)
    for _ in range(200):
        start = date(2025, 1, 1) + timedelta(days=rnd.randint(0, 400))
        end = start + timedelta(days=rnd.randint(0, 800))
        _check(fn(start, end, cap), start, end, cap)


def test_known_windows() -> None:
    assert split_range(date(2026, 1, 1), date(2026, 1, 31), 31) == [(date(2026, 1, 1), date(2026, 1, 31))]
    assert split_range(date(2026, 1, 1), date(2026, 2, 1), 31) == [
        (date(2026, 1, 1), date(2026, 1, 31)), (date(2026, 2, 1), date(2026, 2, 1))]
    assert len(split_range(date(2025, 10, 28), date(2026, 10, 28), 28)) == 14  # 366 days
    assert split_range(date(2026, 1, 2), date(2026, 1, 1), 28) == []
    assert split_range(date(2026, 1, 1), date(2026, 1, 1), 28) == [(date(2026, 1, 1), date(2026, 1, 1))]


def test_aligned_chunks_are_stable_across_days() -> None:
    """Resuming a backfill tomorrow must produce the same keys for the historical chunks."""
    today = date(2026, 10, 28)
    a = split_aligned(today - timedelta(days=365), today, 28)
    b = split_aligned(today - timedelta(days=364), today + timedelta(days=1), 28)
    assert set(a[1:-1]) <= set(b)


def test_bad_cap() -> None:
    with pytest.raises(ValueError):
        split_range(date(2026, 1, 1), date(2026, 1, 2), 0)


def test_range_key_roundtrip() -> None:
    a, b = date(2026, 1, 1), date(2026, 1, 31)
    assert parse_range_key(range_key(a, b)) == (a, b)
