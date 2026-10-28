from __future__ import annotations

from datetime import timedelta

from fakes import FakeClock, FakeGarmin, range_days

from garmin_coach.garmin.client import BadRequestError
from garmin_coach.garmin.endpoints import REFRESH_TIERS, Tier
from garmin_coach.settings import Settings
from garmin_coach.store import repo
from garmin_coach.sync import normalize
from garmin_coach.sync.planner import Planner
from garmin_coach.sync.runner import Runner


def run(conn, settings, fake, clock, tiers, **kw):  # type: ignore[no-untyped-def]
    return Runner(conn, settings, fake, clock=clock).run(set(tiers), **kw)


def test_backfill_within_200_calls_and_complete(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    res = run(conn, settings, fake, clock, {Tier.H})
    assert res.status == "done", res.error
    assert fake.calls_made <= 200, fake.counts()
    assert not repo.is_cache_empty(conn)
    assert {Tier.H, Tier.A, Tier.C, Tier.E} <= res.tiers_completed
    # caps respected
    for name, args in fake.log:
        if "start" in args and "end" in args:
            cap = {"sleep.range": 31, "hrv.range": 28, "stress.range": 28, "steps.range": 28,
                   "bodybattery.range": 31, "hydration.range": 31, "weight.all": 31}.get(name)
            if cap:
                assert range_days(f"{args['start']}..{args['end']}") <= cap
    # detail only for the last 28 days
    detail_ids = {a["activity_id"] for n, a in fake.log if n == "activity.detail"}
    recent = {a["activityId"] for a in fake.activities if a["startTimeLocal"][:10] >= "2026-09-30"}
    assert detail_ids == recent
    assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == len(
        [a for a in fake.activities if a["startTimeLocal"][:10] >= "2025-10-28"])
    assert conn.execute("SELECT COUNT(*) FROM sleep").fetchone()[0] >= 365


def test_backfill_resume_does_not_refetch(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    fake.budget = 60
    first = run(conn, settings, fake, clock, {Tier.H})
    assert first.status == "partial"
    fetched_first = {(n, str(sorted(a.items()))) for n, a in fake.log}
    assert repo.is_cache_empty(conn)
    fake2 = FakeGarmin()
    second = run(conn, settings, fake2, clock, {Tier.H})
    assert second.status == "done"
    refetched = {(n, str(sorted(a.items()))) for n, a in fake2.log} & fetched_first
    assert refetched == set()
    assert fake.calls_made + fake2.calls_made <= 200


def test_backfill_resume_next_day_reuses_chunks(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    fake.budget = 80
    run(conn, settings, fake, clock, {Tier.H})
    clock.advance(days=1)
    fake2 = FakeGarmin()
    run(conn, settings, fake2, clock, {Tier.H})
    assert fake.calls_made + fake2.calls_made <= 230


def test_sync_twice_second_makes_zero_calls(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    run(conn, settings, fake, clock, {Tier.H})
    first = FakeGarmin()
    r1 = run(conn, settings, first, clock, set(REFRESH_TIERS))
    assert r1.status == "done"
    clock.advance(minutes=1)
    second = FakeGarmin()
    r2 = run(conn, settings, second, clock, set(REFRESH_TIERS))
    assert r2.status == "done" and second.calls_made == 0


def test_force_ab_at_most_10_calls(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    run(conn, settings, fake, clock, {Tier.H})
    run(conn, settings, FakeGarmin(), clock, {Tier.A, Tier.B})
    clock.advance(minutes=1)
    forced = FakeGarmin()
    res = run(conn, settings, forced, clock, {Tier.A, Tier.B}, force=True)
    assert res.status == "done"
    assert forced.calls_made <= 10, forced.counts()


def test_new_activity_gets_detail(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    run(conn, settings, fake, clock, {Tier.H})
    clock.advance(hours=2)
    nxt = FakeGarmin()
    new = dict(nxt.activities[0], activityId=3_000_000, startTimeLocal="2026-10-28 09:00:00")
    nxt.activities.insert(0, new)
    run(conn, settings, nxt, clock, {Tier.A})
    c = nxt.counts()
    assert c["activities.search"] == 1 and c["activity.detail"] == 1 and c["activity.workouts"] == 1
    assert conn.execute("SELECT has_detail FROM activity WHERE activity_id=3000000").fetchone()[0] == 1


def test_lazy_detail_for_old_activity(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    run(conn, settings, fake, clock, {Tier.H})
    old = fake.activities[-30]["activityId"]
    assert conn.execute("SELECT has_detail FROM activity WHERE activity_id=?", (old,)).fetchone()[0] == 0
    planner = Planner(conn, settings, clock())
    calls = planner.lazy_detail_calls(old)
    lazy = FakeGarmin()
    Runner(conn, settings, lazy, clock=clock).run({Tier.A}, calls=calls)
    assert lazy.calls_made == 2
    assert conn.execute("SELECT has_detail FROM activity WHERE activity_id=?", (old,)).fetchone()[0] == 1
    assert Planner(conn, settings, clock()).lazy_detail_calls(old) == []


def test_400_halves_window_and_learns_cap(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    def reject_wide(args) -> None:  # type: ignore[no-untyped-def]
        if range_days(f"{args['start']}..{args['end']}") > 14:
            raise BadRequestError("too wide", 400)

    fake.errors["hrv.range"] = reject_wide
    res = run(conn, settings, fake, clock, {Tier.H})
    assert res.status == "done"
    assert repo.learned_cap(conn, "hrv.range") is not None and repo.learned_cap(conn, "hrv.range") <= 14
    assert conn.execute("SELECT COUNT(*) FROM hrv").fetchone()[0] >= 365


def test_404_is_tombstone_not_failure(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    today = clock().astimezone().date().isoformat()
    fake.no_data_days.add(today)
    res = run(conn, settings, fake, clock, {Tier.B})
    assert res.status == "done" and Tier.B in res.tiers_completed
    st = repo.get_sync_state(conn, "sleep.daily", today)
    assert st["last_status"] == 404
    # today's 404 follows the TTL (watch may sync later), it is not frozen for 24 h
    clock.advance(minutes=61)
    again = FakeGarmin()
    run(conn, settings, again, clock, {Tier.B})
    assert again.counts()["sleep.daily"] == 1


def test_normalize_all_rebuilds_identical_tables(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    run(conn, settings, fake, clock, {Tier.H})
    for i in range(3):  # several refreshes with revised data and repeated identical bodies
        clock.advance(hours=3)
        f = FakeGarmin()
        f.version = i % 2
        run(conn, settings, f, clock, set(REFRESH_TIERS), max_age={t: timedelta(0) for t in REFRESH_TIERS})

    def snapshot() -> dict[str, list[tuple]]:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                for t in normalize.NORMALIZED_TABLES}

    before = snapshot()
    assert before["daily"] and before["hrv"] and before["weight"] and before["profile_snapshot"]
    normalize.rebuild_all(conn)
    assert snapshot() == before


def test_raw_dedupe_by_hash(conn, clock: FakeClock) -> None:
    a = repo.save_raw(conn, "sleep.daily", "2026-10-27", {"x": 1}, clock())
    b = repo.save_raw(conn, "sleep.daily", "2026-10-27", {"x": 1}, clock())
    c = repo.save_raw(conn, "sleep.daily", "2026-10-27", {"x": 2}, clock())
    assert a == (a[0], True) and b == (a[0], False) and c[1] is True
    assert conn.execute("SELECT COUNT(*) FROM raw_payload").fetchone()[0] == 2


def test_endpoint_rejecting_every_window_is_disabled_not_cascaded(conn, settings: Settings, fake: FakeGarmin,
                                                                   clock: FakeClock) -> None:
    """Regression from the first live backfill: weight.all answered 400 to every window, down to 1 day."""
    fake.errors["weight.all"] = BadRequestError("rejected", 400)
    res = run(conn, settings, fake, clock, {Tier.H})
    assert res.status == "done" and not repo.is_cache_empty(conn)
    assert fake.counts()["weight.all"] <= 4  # 31 -> 16 -> 8 -> 4 days, then the endpoint is disabled
    assert repo.endpoint_rejected(conn, "weight.all", clock())
    clock.advance(hours=25)
    again = FakeGarmin()
    run(conn, settings, again, clock, {Tier.C})
    assert again.counts()["weight.all"] == 0 and again.counts()["weight.range"] == 1
    clock.advance(days=7)
    probe = FakeGarmin()
    run(conn, settings, probe, clock, {Tier.C})
    assert probe.counts()["weight.all"] == 1  # re-probed after REJECT_DAYS


def test_killed_run_leaves_normalized_tables_consistent(conn, settings: Settings, fake: FakeGarmin,
                                                        clock: FakeClock) -> None:
    """Regression from the live kill test: payloads fetched before the kill must already be normalized."""
    import pytest

    n = {"calls": 0}

    def die_on_40th(args) -> None:  # type: ignore[no-untyped-def]
        n["calls"] += 1
        if n["calls"] == 40:
            raise KeyboardInterrupt  # stands in for the process being killed

    fake.errors["*"] = die_on_40th
    with pytest.raises(KeyboardInterrupt):
        run(conn, settings, fake, clock, {Tier.H})

    def snapshot() -> dict[str, list[tuple]]:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                for t in normalize.NORMALIZED_TABLES}

    before = snapshot()
    assert before["activity"] and before["sleep"]
    normalize.rebuild_all(conn)
    assert snapshot() == before
