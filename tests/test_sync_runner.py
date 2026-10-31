from __future__ import annotations

from datetime import timedelta

import pytest
from fakes import FakeClock, FakeGarmin, range_days

from garmin_coach.garmin.client import BadRequestError, ServerError
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
    calls = planner.lazy_activity_calls(old, want_fit=False)
    lazy = FakeGarmin()
    Runner(conn, settings, lazy, clock=clock).run({Tier.A}, calls=calls)
    assert lazy.calls_made == 2
    assert conn.execute("SELECT has_detail FROM activity WHERE activity_id=?", (old,)).fetchone()[0] == 1
    assert Planner(conn, settings, clock()).lazy_activity_calls(old, want_fit=False) == []
    with_fit = Planner(conn, settings, clock()).lazy_activity_calls(old, want_fit=True)
    assert [c.endpoint for c in with_fit] == ["activity.download"]


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


def test_learned_cap_only_after_success_and_never_below_7_days(conn, settings: Settings, fake: FakeGarmin,
                                                              clock: FakeClock) -> None:
    """Regression: a stale one-day learned cap turned a 4000-day backfill into ~4900 planned calls."""
    repo.set_learned_cap(conn, "sleep.range", 1, clock())  # left over by the old cascade
    calls = Planner(conn, settings, clock()).plan({Tier.H})
    assert sum(c.endpoint == "sleep.range" for c in calls) <= 366 // 7 + 2

    conn.execute("DELETE FROM endpoint_cap")
    fake.errors["weight.all"] = BadRequestError("rejected", 400)
    run(conn, settings, fake, clock, {Tier.H})
    assert repo.learned_cap(conn, "weight.all") == 0  # rejected, not "learned 4 days"

    def reject_wide(args) -> None:  # type: ignore[no-untyped-def]
        if range_days(f"{args['start']}..{args['end']}") > 20:
            raise BadRequestError("too wide", 400)

    conn.execute("DELETE FROM endpoint_cap")
    conn.execute("DELETE FROM sync_state WHERE endpoint='hrv.range'")
    f2 = FakeGarmin()
    f2.errors["hrv.range"] = reject_wide
    run(conn, settings, f2, clock, {Tier.H})
    cap = repo.learned_cap(conn, "hrv.range")
    assert cap is not None and 7 <= cap <= 20  # learned from a window that worked, below the rejected size


def test_server_error_on_wide_window_is_split(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    """Seen live: usersummary.range answered 500 to full-year windows for 2024 and 2025."""
    from garmin_coach.garmin.client import ServerError

    def fail_wide(args) -> None:  # type: ignore[no-untyped-def]
        if range_days(f"{args['start']}..{args['end']}") > 200:
            raise ServerError("HTTP 500 after 5 tries", 500)

    fake.errors["usersummary.range"] = fail_wide
    res = run(conn, settings, fake, clock, {Tier.H})
    assert res.status == "done" and not repo.is_cache_empty(conn)
    assert conn.execute("SELECT COUNT(*) FROM daily WHERE rhr IS NOT NULL").fetchone()[0] >= 365
    cap = repo.learned_cap(conn, "usersummary.range")
    assert cap is not None and 7 <= cap <= 200


# -- FIT downloads (D-071, D-073) -------------------------------------------------------------------------------------
def new_activity(fake: FakeGarmin, aid: int = 3_000_000, when: str = "2026-10-28 09:00:00",
                 type_key: str = "running") -> None:
    fake.activities.insert(0, dict(fake.activities[0], activityId=aid, startTimeLocal=when,
                                   activityType={"typeKey": type_key}))


def test_new_activity_gets_detail_and_fit(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock,
                                          paths) -> None:  # type: ignore[no-untyped-def]
    run(conn, settings, fake, clock, {Tier.H})
    clock.advance(hours=2)
    nxt = FakeGarmin()
    new_activity(nxt)
    res = Runner(conn, settings, nxt, clock=clock, paths=paths).run({Tier.A})
    assert res.status == "done" and Tier.A in res.tiers_completed
    assert [n for n, _ in nxt.log] == ["activities.search", "activity.detail", "activity.workouts",
                                       "activity.download"]
    assert conn.execute("SELECT has_fit FROM activity WHERE activity_id=3000000").fetchone()[0] == 1
    assert (paths.fit_dir / "3000000.zip").exists()
    assert conn.execute("SELECT COUNT(*) FROM raw_payload WHERE endpoint='activity.download'").fetchone()[0] == 0
    assert repo.get_sync_state(conn, "activity.download", "3000000")["last_status"] == 200


@pytest.mark.parametrize("kind", ["404", "parse_error", "5xx"])
def test_failed_download_never_stales_tier_a(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock,
                                             kind: str) -> None:
    run(conn, settings, fake, clock, {Tier.H})
    clock.advance(hours=2)
    nxt = FakeGarmin()
    new_activity(nxt)
    if kind == "404":
        nxt.no_fit_ids.add(3_000_000)
    elif kind == "parse_error":
        nxt.bad_zip_ids.add(3_000_000)
    else:
        nxt.errors["activity.download"] = ServerError("activity.download: HTTP 503 after 5 tries", 503)
    res = run(conn, settings, nxt, clock, {Tier.A})
    assert Tier.A in res.tiers_completed
    assert repo.tier_states(conn)["A"]["last_completed_at"] == clock()
    st = repo.get_sync_state(conn, "activity.download", "3000000")
    fit_row = conn.execute("SELECT status FROM activity_fit WHERE activity_id=3000000").fetchone()
    if kind == "404":
        assert res.status == "done" and st["last_status"] == 404 and fit_row["status"] == "not_found"
    elif kind == "parse_error":
        assert res.status == "done" and st["last_status"] == 200 and fit_row["status"] == "parse_error"
    else:
        assert res.status == "partial" and st["last_status"] == 503 and fit_row is None
    clock.advance(hours=2)
    again = FakeGarmin()
    new_activity(again)
    run(conn, settings, again, clock, {Tier.A})
    # tried once: never refetched; a 5xx is retried on the next plan
    assert again.counts()["activity.download"] == (1 if kind == "5xx" else 0)


def test_budget_stop_defers_downloads_and_next_refresh_picks_them_up(conn, settings: Settings, fake: FakeGarmin,
                                                                       clock: FakeClock) -> None:
    run(conn, settings, fake, clock, {Tier.H})
    clock.advance(hours=2)
    nxt = FakeGarmin(budget=6)
    for i in range(2):
        new_activity(nxt, 3_000_000 + i, f"2026-10-28 0{8 + i}:00:00")
    res = run(conn, settings, nxt, clock, {Tier.A})
    assert res.status == "partial" and Tier.A in res.tiers_completed and Tier.F not in res.tiers_completed
    # search + 2 x (detail + workouts) complete A first; one download fits in the budget, the other waits
    names = [n for n, _ in nxt.log]
    assert names[:5].count("activity.download") == 0 and names[5:] == ["activity.download"]
    clock.advance(hours=2)
    later = FakeGarmin()
    for i in range(2):
        new_activity(later, 3_000_000 + i, f"2026-10-28 0{8 + i}:00:00")
    run(conn, settings, later, clock, {Tier.A})
    assert later.counts()["activity.download"] == 1 and later.counts()["activity.detail"] == 0


def test_backfill_details_before_downloads_across_pages(conn, settings: Settings, clock: FakeClock) -> None:
    settings.sync.detail_days = 400  # every activity of the year gets detail + FIT: several list pages
    fake = FakeGarmin()
    run(conn, settings, fake, clock, {Tier.H})
    names = [n for n, _ in fake.log]
    first_download = names.index("activity.download")
    assert "activity.detail" not in names[first_download:] and "activities.search" not in names[first_download:]
    assert names.count("activity.download") == names.count("activity.detail") > 100


def test_fit_disabled_and_skip_types(conn, settings: Settings, fake: FakeGarmin, clock: FakeClock) -> None:
    settings.fit.download = False
    run(conn, settings, fake, clock, {Tier.H})
    assert fake.counts()["activity.download"] == 0 and fake.counts()["activity.detail"] > 0
    settings.fit.download = True
    settings.fit.skip_types = ["running"]
    clock.advance(hours=2)
    nxt = FakeGarmin()
    new_activity(nxt, 3_000_000, type_key="running")
    new_activity(nxt, 3_000_001, "2026-10-28 10:00:00", type_key="road_biking")
    run(conn, settings, nxt, clock, {Tier.A})
    assert [a["activity_id"] for n, a in nxt.log if n == "activity.download"] == [3_000_001]


def test_tier_f_plans_the_whole_history_newest_first(conn, settings: Settings, fake: FakeGarmin,
                                                     clock: FakeClock) -> None:
    settings.fit.download = False
    run(conn, settings, fake, clock, {Tier.H})
    settings.fit.download = True
    calls = Planner(conn, settings, clock()).plan({Tier.F})
    starts = [conn.execute("SELECT start_local FROM activity WHERE activity_id=?", (c.args["activity_id"],))
              .fetchone()[0] for c in calls]
    assert len(calls) == conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] > 100
    assert starts == sorted(starts, reverse=True) and all(c.tier == Tier.F for c in calls)
    assert calls[0].step == f"activity.download {calls[0].key} ({starts[0][:10]})"


def test_yield_check_stops_partial_and_keeps_the_call(conn, settings: Settings, fake: FakeGarmin,
                                                      clock: FakeClock) -> None:
    settings.fit.download = False
    run(conn, settings, fake, clock, {Tier.H})
    settings.fit.download = True
    n_pending = len(Planner(conn, settings, clock()).plan({Tier.F}))
    f = FakeGarmin()
    res = Runner(conn, settings, f, clock=clock, yield_check=lambda: f.calls_made >= 3).run({Tier.F})
    assert res.status == "partial" and res.error == "yielded: on-demand refresh waiting" and res.calls_done == 3
    assert len(Planner(conn, settings, clock()).plan({Tier.F})) == n_pending - 3  # the 4th was not lost
    rest = FakeGarmin()
    assert run(conn, settings, rest, clock, {Tier.F}).status == "done" and rest.calls_made == n_pending - 3
