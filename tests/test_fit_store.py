"""activity_fit rows, kept files, re-parse, pending list, coverage, schema v4 (PLAN-FIT §4)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import fitgen
from fakes import FakeClock, FakeGarmin

from garmin_coach.fit import store
from garmin_coach.garmin.endpoints import Tier
from garmin_coach.settings import Paths, Settings
from garmin_coach.store import db, repo
from garmin_coach.sync import normalize
from garmin_coach.sync.runner import Runner

TODAY = date(2026, 10, 28)


def add_activity(conn: sqlite3.Connection, aid: int, day: str, type_key: str = "road_biking") -> None:
    conn.execute("INSERT INTO activity(activity_id, start_local, type_key) VALUES (?,?,?)", (aid, f"{day} 07:00:00",
                                                                                              type_key))


def row(conn: sqlite3.Connection, aid: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM activity_fit WHERE activity_id=?", (aid,)).fetchone()


def has_fit(conn: sqlite3.Connection, aid: int) -> int:
    return conn.execute("SELECT has_fit FROM activity WHERE activity_id=?", (aid,)).fetchone()[0]


def test_ingest_writes_file_row_and_flag(conn, paths: Paths, settings: Settings, clock: FakeClock) -> None:
    add_activity(conn, 1, "2026-10-27")
    data = fitgen.activity_zip(1)
    assert store.ingest(conn, paths, settings, 1, data, clock()) == "ok"
    r = row(conn, 1)
    assert r["status"] == "ok" and r["file_name"] == "1.zip" and r["parser_version"] == store.PARSER_VERSION
    assert (paths.fit_dir / "1.zip").read_bytes() == data and r["zip_bytes"] == len(data)
    assert (r["sport"], r["sub_sport"], r["device"], r["n_sessions"], r["n_laps"]) == ("cycling", "road", "fenix7", 1, 11)
    assert r["fetched_at"] == r["parsed_at"] == "2026-10-28T10:00:00+00:00" and r["error"] is None
    assert json.loads(r["data"])["session"]["sport"] == "cycling"
    assert store.summary(conn, 1)["meta"]["n_laps"] == 11  # type: ignore[index]
    assert has_fit(conn, 1) == 1
    assert not list(paths.fit_dir.glob("*.tmp"))  # atomic write leaves no temp file


def test_keep_files_false_writes_nothing(conn, paths: Paths, settings: Settings, clock: FakeClock) -> None:
    settings.fit.keep_files = False
    add_activity(conn, 2, "2026-10-27")
    assert store.ingest(conn, paths, settings, 2, fitgen.activity_zip(2), clock()) == "ok"
    assert row(conn, 2)["file_name"] is None and not paths.fit_dir.exists()


def test_bad_files_and_not_found(conn, paths: Paths, settings: Settings, clock: FakeClock) -> None:
    for aid in (3, 4, 5):
        add_activity(conn, aid, "2026-10-26")
    assert store.ingest(conn, paths, settings, 3, b"garbage" * 50, clock()) == "parse_error"
    assert store.ingest(conn, paths, settings, 4, fitgen.zip_bytes("4.gpx", fitgen.GPX), clock()) == "no_fit"
    store.mark(conn, 5, "not_found", clock())
    assert row(conn, 3)["error"] == "not a ZIP or FIT file" and row(conn, 3)["data"] is None
    assert row(conn, 4)["error"] == "no .fit file in the archive"
    r5 = row(conn, 5)
    assert r5["status"] == "not_found" and r5["file_name"] is None and r5["parser_version"] is None
    assert [has_fit(conn, a) for a in (3, 4, 5)] == [0, 0, 0]
    assert store.summary(conn, 3) is None
    assert store.activity_status(conn, settings, 3, "road_biking") == "parse_error"
    assert store.activity_status(conn, settings, 4, "road_biking") == "no_file"
    assert store.activity_status(conn, settings, 5, "road_biking") == "no_file"


def test_reparse_bumps_version_and_fixes_parse_errors(conn, paths: Paths, settings: Settings,
                                                      clock: FakeClock) -> None:
    add_activity(conn, 6, "2026-10-27")
    add_activity(conn, 7, "2026-10-27")
    store.ingest(conn, paths, settings, 6, fitgen.activity_zip(6), clock())
    store.ingest(conn, paths, settings, 7, b"not yet readable" * 10, clock())
    conn.execute("UPDATE activity_fit SET parser_version=0 WHERE activity_id=6")  # as if written by an older parser
    store.mark(conn, 8, "not_found", clock())
    todo, missing = store.reparse_candidates(conn)
    assert todo == [7, 6] and missing == []
    (paths.fit_dir / "7.zip").write_bytes(fitgen.activity_zip(7))  # "a parser fix" for the broken file
    clock.advance(hours=1)
    assert [store.reparse(conn, paths, settings, a, clock()) for a in todo] == ["ok", "ok"]
    r6 = row(conn, 6)
    assert r6["parser_version"] == store.PARSER_VERSION and r6["parsed_at"] > r6["fetched_at"]
    assert has_fit(conn, 7) == 1
    assert store.reparse_candidates(conn) == ([], [])
    assert store.reparse_candidates(conn, everything=True)[0] == [7, 6]
    (paths.fit_dir / "6.zip").unlink()
    assert store.reparse(conn, paths, settings, 6, clock()) == "no_file"
    assert store.reparse(conn, paths, settings, 8, clock()) == "no_file"  # not_found: nothing kept


def test_pending_ids_order_and_filters(conn, settings: Settings, clock: FakeClock) -> None:
    add_activity(conn, 10, "2026-10-01")
    add_activity(conn, 11, "2026-10-20", "walking")
    add_activity(conn, 12, "2026-10-25")
    add_activity(conn, 13, "2025-01-10")
    add_activity(conn, 14, "2026-10-26")
    add_activity(conn, 15, "2026-10-27")
    add_activity(conn, 16, "2026-10-24")
    repo.set_sync_state(conn, "activity.download", "14", 200, clock())
    repo.set_sync_state(conn, "activity.download", "15", 404, clock())
    repo.set_sync_state(conn, "activity.download", "16", 400, clock())  # a client error is not retried
    repo.set_sync_state(conn, "activity.download", "12", 500, clock())  # a server error is
    assert store.pending_ids(conn, settings, TODAY) == [12, 11, 10, 13]
    settings.fit.skip_types = ["walking"]
    assert store.pending_ids(conn, settings, TODAY) == [12, 10, 13]
    settings.fit.backfill_days = 30
    assert store.pending_ids(conn, settings, TODAY) == [12, 10]


def test_coverage(conn, paths: Paths, settings: Settings, clock: FakeClock) -> None:
    for aid in (20, 21, 22, 23):
        add_activity(conn, aid, "2026-10-2" + str(aid - 20))
    store.ingest(conn, paths, settings, 20, fitgen.activity_zip(20), clock())
    store.ingest(conn, paths, settings, 21, b"x" * 100, clock())
    store.mark(conn, 22, "not_found", clock())
    for aid in (20, 21, 22):
        repo.set_sync_state(conn, "activity.download", str(aid), 404 if aid == 22 else 200, clock())
    cov = store.coverage(conn, settings, paths, TODAY)
    assert cov == {"enabled": True, "ok": 1, "parse_error": 1, "no_file": 1, "pending": 1,
                   "files_mb": round(((paths.fit_dir / "20.zip").stat().st_size + 100) / 1_048_576, 1)}


def test_rebuild_all_keeps_activity_fit_and_restores_has_fit(conn, settings: Settings, fake: FakeGarmin,
                                                             clock: FakeClock, paths: Paths) -> None:
    Runner(conn, settings, fake, clock=clock, paths=paths).run({Tier.H})
    ok_ids = {r[0] for r in conn.execute("SELECT activity_id FROM activity_fit WHERE status='ok'")}
    assert ok_ids and ok_ids == {r[0] for r in conn.execute("SELECT activity_id FROM activity WHERE has_fit=1")}
    before = conn.execute("SELECT * FROM activity_fit ORDER BY 1").fetchall()
    normalize.rebuild_all(conn)
    assert ok_ids == {r[0] for r in conn.execute("SELECT activity_id FROM activity WHERE has_fit=1")}
    assert [tuple(r) for r in conn.execute("SELECT * FROM activity_fit ORDER BY 1")] == [tuple(r) for r in before]


def test_v3_database_migrates_to_v4_without_touching_rows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("DROP TABLE activity_fit")
    conn.execute("DROP TABLE app_flag")
    conn.execute("PRAGMA user_version = 3")
    conn.execute("INSERT INTO activity(activity_id, start_local, type_key, name) VALUES (1, '2026-10-01', 'running', 'x')")
    repo.save_raw(conn, "activities.search", "recent:0:20", [{"activityId": 1}], FakeClock()())
    conn.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 4
    assert conn.execute("SELECT name FROM activity WHERE activity_id=1").fetchone()[0] == "x"
    assert conn.execute("SELECT COUNT(*) FROM raw_payload").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM activity_fit").fetchone()[0] == 0
    repo.set_flag(conn, "k", "v")
    assert repo.get_flag(conn, "k") == "v"
    conn.close()
