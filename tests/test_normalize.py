from __future__ import annotations

from fakes import fixture

from garmin_coach.store import repo
from garmin_coach.sync import normalize


def load(conn, clock, endpoint: str, key: str, name: str) -> None:  # type: ignore[no-untyped-def]
    rid, _ = repo.save_raw(conn, endpoint, key, fixture(name), clock())
    normalize.apply(conn, [rid])


def row(conn, sql: str):  # type: ignore[no-untyped-def]
    r = conn.execute(sql).fetchone()
    return dict(r) if r else None


def test_activity(conn, clock) -> None:  # type: ignore[no-untyped-def]
    rid, _ = repo.save_raw(conn, "activities.search", "recent:0:20", [fixture("activity_summary")], clock())
    normalize.apply(conn, [rid])
    a = row(conn, "SELECT * FROM activity")
    assert a["activity_id"] == 1000001 and a["type_key"] == "running"
    assert a["start_local"] == "2026-10-27T07:05:00" and a["start_gmt"] == "2026-10-27T06:05:00"
    assert a["training_load"] == 142.6 and a["vo2max"] == 54.0 and a["lthr"] == 166 and a["ftp"] is None
    assert a["aerobic_te"] == 3.4 and a["te_label"] == "TEMPO" and a["body_battery_diff"] == -14
    assert a["has_detail"] == 0
    rid2, _ = repo.save_raw(conn, "activity.detail", "1000001", {"activityId": 1000001}, clock())
    normalize.apply(conn, [rid2])
    assert row(conn, "SELECT has_detail FROM activity")["has_detail"] == 1


def test_daily_v1_then_v2_coalesce(conn, clock) -> None:  # type: ignore[no-untyped-def]
    load(conn, clock, "usersummary.daily", "2026-10-27", "usersummary_daily")
    d = row(conn, "SELECT * FROM daily WHERE date='2026-10-27'")
    assert d["rhr"] == 47 and d["rhr_7d_avg"] == 48 and d["bb_at_wake"] == 85 and d["stress_high_s"] == 1800
    assert d["intensity_min_vigorous"] == 45 and d["hydration_ml"] is None
    load(conn, clock, "usersummary.range", "2026-10-26..2026-10-26", "usersummary_range")
    d2 = row(conn, "SELECT * FROM daily WHERE date='2026-10-26'")
    assert d2["steps"] == 9100 and d2["distance_m"] == 7300.0 and d2["rhr"] == 48
    assert d2["stress_high_s"] == 2400 and d2["stress_rest_s"] == 28000 and d2["bb_high"] == 80
    assert d2["hydration_ml"] == 1500.0 and d2["spo2_avg"] == 95


def test_daily_range_sources(conn, clock) -> None:  # type: ignore[no-untyped-def]
    load(conn, clock, "stress.range", "k", "stress_range")
    load(conn, clock, "steps.range", "k", "steps_range")
    load(conn, clock, "hydration.range", "k", "hydration_range")
    load(conn, clock, "bodybattery.range", "k", "bodybattery_range")
    load(conn, clock, "hr.daily", "2026-10-27", "hr_daily")
    d = row(conn, "SELECT * FROM daily WHERE date='2026-10-26'")
    assert d["stress_avg"] == 31 and d["steps"] == 9100 and d["hydration_ml"] == 1500.0
    d27 = row(conn, "SELECT * FROM daily WHERE date='2026-10-27'")
    assert d27["bb_charged"] == 62 and d27["rhr"] == 47 and d27["hr_min"] == 43


def test_sleep(conn, clock) -> None:  # type: ignore[no-untyped-def]
    load(conn, clock, "sleep.daily", "2026-10-27", "sleep_daily")
    s = row(conn, "SELECT * FROM sleep")
    assert s["date"] == "2026-10-27" and s["total_s"] == 27600 and s["deep_s"] == 5400 and s["rem_s"] == 7200
    assert s["score"] == 84 and s["score_quality"] == "GOOD" and s["sleep_need_min"] == 500
    assert s["start_local"] == "2026-10-26T23:00:00" and s["end_local"] == "2026-10-27T07:00:00" and s["feedback"] == "POSITIVE_LONG_AND_DEEP"
    # Connect-web style wrapper is accepted too
    rid, _ = repo.save_raw(conn, "sleep.daily", "2026-10-28",
                           {"dailySleepDTO": {**fixture("sleep_daily"), "calendarDate": "2026-10-28"}}, clock())
    normalize.apply(conn, [rid])
    assert row(conn, "SELECT score FROM sleep WHERE date='2026-10-28'")["score"] == 84


def test_hrv_baseline_fields(conn, clock) -> None:  # type: ignore[no-untyped-def]
    load(conn, clock, "hrv.daily", "2026-10-27", "hrv_daily")
    load(conn, clock, "hrv.range", "2026-10-25..2026-10-25", "hrv_range")
    h = row(conn, "SELECT * FROM hrv WHERE date='2026-10-27'")
    assert h["last_night_avg"] == 58 and h["weekly_avg"] == 62 and h["last_night_5min_high"] == 81
    assert (h["baseline_low_upper"], h["baseline_balanced_low"], h["baseline_balanced_upper"]) == (52, 56, 70)
    assert h["status"] == "BALANCED"
    assert row(conn, "SELECT last_night_avg FROM hrv WHERE date='2026-10-25'")["last_night_avg"] == 66


def test_weight_units(conn, clock) -> None:  # type: ignore[no-untyped-def]
    load(conn, clock, "weight.all", "k", "weight_all")
    load(conn, clock, "weight.range", "k", "weight_range")  # same weigh-in reported in kg -> same grams
    rows = conn.execute("SELECT * FROM weight").fetchall()
    assert len(rows) == 1
    w = dict(rows[0])
    assert w["weight_g"] == 72400.0 and w["date"] == "2026-10-20" and w["body_fat_pct"] == 14.2


def test_profile_snapshots(conn, clock) -> None:  # type: ignore[no-untyped-def]
    load(conn, clock, "hrzones", "-", "hrzones")
    load(conn, clock, "profile.settings", "-", "profile_settings")
    load(conn, clock, "hrzones", "-", "hrzones")  # identical body -> no new snapshot
    kinds = [r[0] for r in conn.execute("SELECT kind FROM profile_snapshot ORDER BY kind")]
    assert kinds == ["hr_zones", "settings"]


def test_garbage_never_crashes(conn, clock) -> None:  # type: ignore[no-untyped-def]
    ids = [repo.save_raw(conn, ep, "k", body, clock())[0] for ep, body in [
        ("sleep.daily", "not a dict"), ("hrv.range", {"hrvSummaries": None}), ("activities.search", [1, None, {}]),
        ("usersummary.range", {"data": [{"calendarDate": None}]}), ("weight.all", {"x": 1}),
    ]]
    normalize.apply(conn, ids)
    assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 0
