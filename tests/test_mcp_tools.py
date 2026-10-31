"""MCP tools in-process against a fixture DB with a fake Garmin client (PLAN Phase 3.4). No network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from fakes import FakeClock, FakeGarmin
from test_context import STATE, write_inputs, write_state

from garmin_coach import state as state_m
from garmin_coach.app import App
from garmin_coach.garmin.client import NetworkError, ServerError
from garmin_coach.garmin.endpoints import REFRESH_TIERS, Tier
from garmin_coach.mcp import prompts as prompts_mod
from garmin_coach.mcp import tools as tools_mod
from garmin_coach.mcp.server import build_server
from garmin_coach.mcp.tools import CoachService
from garmin_coach.secrets import KeyFileStatus
from garmin_coach.settings import STATE_FILE, Paths, Settings
from garmin_coach.store import db, repo
from garmin_coach.sync import jobs as jobs_mod
from garmin_coach.sync.runner import Runner
from garmin_coach.sync.startup import startup_check

REPO = Path(__file__).resolve().parents[1]


class Fetchers:
    """Records every fake handed to the freshness gate (budget, timeout) -> fetcher."""

    def __init__(self, broken: bool = False) -> None:
        self.broken = broken
        self.made: list[FakeGarmin] = []
        self.lock = threading.Lock()
        self.setup: Callable[[FakeGarmin], None] | None = None

    def __call__(self, budget: int, timeout: float | None = None, gap_ms: int | None = None) -> FakeGarmin:
        f = FakeGarmin(budget=budget)
        if self.broken:
            f.errors["*"] = NetworkError("connection refused")
        if self.setup is not None:
            self.setup(f)
        with self.lock:
            self.made.append(f)
        return f

    @property
    def calls(self) -> int:
        return sum(f.calls_made for f in self.made)


def _key(source: str) -> KeyFileStatus:
    return KeyFileStatus(Path("~/.config/garmin/connect-personal-api-key"), source != "missing", source, None, 3)


@pytest.fixture(autouse=True)
def _key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_mod, "key_file_status", lambda: _key("file"))


def make_app(paths: Paths, settings: Settings, clock: FakeClock, fetchers: Fetchers,
             job_slow: float = 0.0) -> App:
    app = App(paths=paths, settings=settings, clock=clock)

    def job_fetcher(kind: str) -> FakeGarmin:
        f = FakeGarmin()
        if job_slow:
            f.errors["*"] = lambda args: time.sleep(job_slow)
        return f

    app.fetcher = fetchers  # type: ignore[method-assign]
    app.job_fetcher = job_fetcher  # type: ignore[method-assign]
    return app


def warm(paths: Paths, settings: Settings, clock: FakeClock) -> None:
    write_inputs(paths)
    conn = db.connect(paths.db)
    Runner(conn, settings, FakeGarmin(), clock=clock).run({Tier.H})
    Runner(conn, settings, FakeGarmin(), clock=clock).run(set(REFRESH_TIERS))
    conn.close()


@pytest.fixture
def warmed(paths: Paths, settings: Settings, clock: FakeClock) -> tuple[CoachService, Fetchers]:
    warm(paths, settings, clock)
    f = Fetchers()
    return CoachService(make_app(paths, settings, clock, f)), f


def wait_idle(svc: CoachService, timeout: float = 30) -> None:
    conn = db.connect(svc.app.paths.db)
    try:
        for j in jobs_mod.active_jobs(conn):
            svc.app.jobs.wait(j["id"], timeout=timeout)
    finally:
        conn.close()


# -- freshness behaviour (PLAN Phase 3.4) ---------------------------------------------------------------------------
def test_stale_refreshes_then_fresh_makes_no_calls(warmed, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    svc, f = warmed
    clock.advance(hours=2)  # A/B stale (TTL 60 min), C/D/E still fresh
    r = svc.get_daily_brief()
    assert "error" not in r, r
    fr = r["freshness"]
    assert fr["tiers"]["A"]["refreshed_now"] and fr["tiers"]["B"]["refreshed_now"] and not fr["stale"]
    assert 0 < f.calls <= 10
    assert r["sleep_last_night"] and r["training_load"] and r["readiness_heuristic"]["score"] is not None
    calls = f.calls
    r2 = svc.get_daily_brief()
    assert f.calls == calls  # second question within the TTL: zero Garmin calls
    assert not r2["freshness"]["tiers"]["A"].get("refreshed_now")
    assert svc.get_sync_status()["tiers"]["A"]["age_minutes"] == 0


def test_max_age_zero_forces_only_what_the_answer_needs(warmed) -> None:  # type: ignore[no-untyped-def]
    svc, f = warmed
    r = svc.get_daily_brief(max_age_minutes=0)  # fresh cache, "I just synced my watch"
    fr = r["freshness"]
    assert fr["tiers"]["A"]["refreshed_now"] and fr["tiers"]["B"]["refreshed_now"] and not fr["stale"]
    assert not fr["tiers"]["C"].get("background_refresh_started")  # C/D keep their TTL
    assert 0 < f.calls <= 10
    conn = db.connect(svc.app.paths.db)
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0
    conn.close()


def test_client_error_serves_stale_cache(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    warm(paths, settings, clock)
    clock.advance(hours=2)
    svc = CoachService(make_app(paths, settings, clock, Fetchers(broken=True)))
    r = svc.get_daily_brief()
    assert r["freshness"]["stale"] and "connection refused" in r["freshness"]["stale_reason"]
    assert r["sleep_last_night"] and r["activities_yesterday"]  # cached data still served
    r2 = svc.get_sleep()
    assert r2["sleep"]["total_s"] > 0 and r2["freshness"]["circuit_open"]


def test_concurrent_calls_make_one_refresh(warmed, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    svc, f = warmed
    clock.advance(hours=2)
    results: list[dict[str, Any]] = []
    threads = [threading.Thread(target=lambda: results.append(svc.get_daily_brief())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2 and all(not r["freshness"]["stale"] for r in results)
    assert sum(1 for fg in f.made if fg.calls_made) == 1
    assert sum(bool(r["freshness"]["tiers"]["B"].get("refreshed_now")) for r in results) == 1


def test_empty_db_startup_backfill_and_history_not_loaded(paths, settings, clock) -> None:  # type: ignore[no-untyped-def]
    write_inputs(paths)
    settings.sync.lock_wait_seconds = 0.2
    svc = CoachService(make_app(paths, settings, clock, Fetchers(), job_slow=0.01))
    jid = startup_check(svc.app.jobs, settings, clock=clock)
    assert jid is not None
    job = jobs_mod.get_job(svc.app.jobs.conn, jid)
    assert job is not None and job["kind"] == "backfill"
    st = svc.get_sync_status()
    assert st["history_loaded"] is False and st["job"]["id"] == jid
    brief = svc.get_daily_brief()
    assert brief["freshness"]["history_loaded"] is False
    assert "Storico" in brief["history_note"]
    assert svc.get_context_pack("daily")["markdown"].count("Storico") >= 1
    wait_idle(svc, 60)
    assert svc.get_sync_status()["history_loaded"] is True
    assert svc.get_events()["freshness"]["history_loaded"] is True


def test_background_tiers_enqueue_one_job(warmed, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    svc, f = warmed
    clock.advance(days=2)
    r = svc.get_daily_brief()
    assert r["freshness"]["tiers"]["C"]["background_refresh_started"]
    svc.get_range_summary()
    conn = db.connect(svc.app.paths.db)
    assert len(conn.execute("SELECT * FROM job").fetchall()) == 1
    conn.close()
    wait_idle(svc)


# -- tools ----------------------------------------------------------------------------------------------------------
def test_get_activity_lazy_detail_does_not_touch_tier_a(warmed, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    svc, f = warmed
    conn = db.connect(svc.app.paths.db)
    old = conn.execute("SELECT activity_id FROM activity WHERE has_detail=0 ORDER BY start_local LIMIT 1").fetchone()[0]
    a_before = repo.tier_states(conn)["A"]["last_completed_at"]
    clock.advance(minutes=5)
    r = svc.get_activity(old, include_splits=True)
    assert "error" not in r and r["activity"]["has_detail"] == 1 and "detail_problem" not in r
    assert [n for fg in f.made for n, _ in fg.log] == ["activity.detail", "activity.workouts", "activity.download"]
    assert r["fit_status"] == "ok" and r["activity"]["has_fit"] == 1 and "fit_problem" not in r
    assert repo.tier_states(conn)["A"]["last_completed_at"] == a_before
    assert repo.get_flag(conn, repo.SYNC_WANTED) is None  # set while waiting for the lock, then cleared
    svc.get_activity(old)
    assert f.calls == 3  # fetched once
    assert "not in the local cache" in svc.get_activity(42)["error"]
    svc.app.settings.fit.download = False
    older = conn.execute("SELECT activity_id FROM activity WHERE has_detail=0 ORDER BY start_local LIMIT 1").fetchone()[0]
    r = svc.get_activity(older)
    assert f.calls == 5 and r["fit_status"] == "disabled" and "fit" not in r  # 2 calls when FIT is disabled
    conn.close()


# -- FIT in the tools (PLAN-FIT §8) -----------------------------------------------------------------------------------
def _recent_with_fit(svc: CoachService) -> int:
    conn = db.connect(svc.app.paths.db)
    try:
        return conn.execute("SELECT activity_id FROM activity WHERE has_fit=1 ORDER BY start_local DESC").fetchone()[0]
    finally:
        conn.close()


def test_get_activity_fit_sections_laps_and_series(warmed, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    svc, f = warmed
    aid = _recent_with_fit(svc)  # downloaded during the history backfill (inside the 28-day detail window)
    r = svc.get_activity(aid)
    assert f.calls == 0 and r["fit_status"] == "ok"
    fit = r["fit"]
    assert set(fit) == {"meta", "session", "zones", "intervals", "dynamics", "records", "route", "device"}
    assert fit["meta"] == {"parser_version": 1, "n_laps": 11, "n_records": 110, "laps_truncated": False,
                           "truncated": False}
    assert fit["intervals"]["work"]["n"] == 6 and fit["zones"]["source"] == "time_in_zone"
    assert "laps" not in fit and "series" not in fit
    full = svc.get_activity(aid, include_laps=True, include_series=True)["fit"]
    assert len(full["laps"]) == 11 and "laps_more" not in full and full["series"]
    monkeypatch.setattr(tools_mod, "MAX_LAPS_OUT", 4)
    cut = svc.get_activity(aid, include_laps=True)["fit"]
    assert len(cut["laps"]) == 4 and cut["laps_more"] is True
    monkeypatch.setattr(tools_mod, "MAX_PAYLOAD_CHARS", 3000)
    assert "too large" in svc.get_activity(aid, include_laps=True, include_series=True)["error"]


@pytest.mark.parametrize("case,expected", [
    ("no_file", "no_file"), ("parse_error", "parse_error"), ("disabled", "disabled"),
    ("skipped_type", "skipped_type"), ("cooldown", "not_downloaded"), ("5xx", "not_downloaded"),
])
def test_fit_status_values(paths, settings, clock, case: str, expected: str) -> None:  # type: ignore[no-untyped-def]
    warm(paths, settings, clock)
    f = Fetchers()
    svc = CoachService(make_app(paths, settings, clock, f))
    conn = db.connect(paths.db)
    old = conn.execute("SELECT activity_id FROM activity WHERE has_detail=0 ORDER BY start_local LIMIT 1").fetchone()[0]
    if case == "no_file":
        f.setup = lambda fg: fg.no_fit_ids.add(old)
    elif case == "parse_error":
        f.setup = lambda fg: fg.bad_zip_ids.add(old)
    elif case == "disabled":
        settings.fit.download = False
    elif case == "skipped_type":
        settings.fit.skip_types = ["running"]
    elif case == "cooldown":
        repo.set_cooldown(conn, ["F"], clock() + timedelta(hours=1), "429s")
    else:
        f.setup = lambda fg: fg.errors.update({"activity.download": ServerError("HTTP 503 after 5 tries", 503)})
    r = svc.get_activity(old)
    conn.close()
    assert r["fit_status"] == expected and "fit" not in r and r["activity"]["has_detail"] == 1
    assert ("fit_problem" in r) == (case == "5xx")
    if case == "5xx":
        assert "HTTP 503" in r["fit_problem"]
    downloads = sum(fg.counts()["activity.download"] for fg in f.made)
    assert downloads == (1 if case in ("no_file", "parse_error", "5xx") else 0)


def test_list_activities_has_fit_and_status_fit_block(warmed) -> None:  # type: ignore[no-untyped-def]
    svc, _ = warmed
    acts = svc.list_activities(limit=100)["activities"]
    assert acts[0]["has_fit"] is True and acts[-1]["has_fit"] is False
    st = svc.get_sync_status()["fit"]
    n_ok = sum(a["has_fit"] for a in svc.list_activities(limit=100)["activities"])
    assert st["enabled"] is True and st["ok"] == n_ok > 0 and st["pending"] > 0 and st["files_mb"] >= 0
    assert st["parse_error"] == 0 and st["no_file"] == 0 and st["cooldown_until"] is None
    assert "job" not in st and "last_job" not in st


def test_refresh_scope_fit_guards(warmed, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    svc, _ = warmed
    r = svc.refresh(scope="fit", force=True)
    assert r["kind"] == "fit" and r["job_id"] and "chained jobs" in r["note"]
    again = svc.refresh(scope="fit")
    assert again.get("job_id") == r["job_id"] or "less than 5 minutes" in again.get("error", "")
    wait_idle(svc, 60)
    assert "less than 5 minutes" in svc.refresh(scope="fit")["error"]
    st = svc.get_sync_status()["fit"]
    assert st["last_job"]["status"] == "done" and st["pending"] == 0
    clock.advance(minutes=6)
    conn = db.connect(svc.app.paths.db)
    repo.set_cooldown(conn, ["F"], clock() + timedelta(hours=1), "429s")
    conn.close()
    assert "cooldown" in svc.refresh(scope="fit")["error"]
    svc.app.settings.fit.download = False
    assert "disabled" in svc.refresh(scope="fit")["error"]
    assert "scope must be" in svc.refresh(scope="everything")["error"]


def test_list_and_range_tools(warmed) -> None:  # type: ignore[no-untyped-def]
    svc, _ = warmed
    acts = svc.list_activities(limit=5)
    assert acts["count"] == 5 and acts["more"] and acts["activities"][0]["id"]
    assert svc.list_activities(type_="swim")["count"] == 0
    assert svc.list_activities(type_="run")["count"] > 0  # fixture activities are runs
    rs = svc.get_range_summary()
    assert len(rs["days"]) == 7 and rs["days"][-1]["date"] == "2026-10-28"
    wk = svc.get_range_summary(start="2026-09-01", end="2026-10-28", granularity="week")
    assert 8 <= len(wk["weeks"]) <= 10 and wk["weeks"][0]["week_start"] == "2026-08-31"
    tl = svc.get_training_load(start="2026-10-01", end="2026-10-28")
    assert len(tl["series"]) == 28 and "ctl" in tl["series"][-1]
    assert svc.get_hrv()["hrv"]["last_night_avg"] and len(svc.get_hrv()["last_7_nights"]) == 7
    bb = svc.get_body_battery(date_="2026-10-27", include_curve=True)
    assert bb["curve"] and bb["curve"][0]["level"] == 30
    hr = svc.get_heart_rate(include_samples=True)
    assert hr["heart_rate"]["resting_bpm"] and hr["samples_10min"]
    assert svc.get_weight()["weigh_ins"]
    assert svc.get_hr_zones()["zones"]


def test_profile_events_and_packs(warmed) -> None:  # type: ignore[no-untyped-def]
    svc, _ = warmed
    p = svc.get_athlete_profile()
    assert p["profile"]["name"] == "Test Athlete" and "Corre dal 2018" in p["history_md"]
    assert "Lunghi" in p["coach_notes_md"] and p["hr_zones"]
    only = svc.get_athlete_profile(["history"])
    assert set(only) == {"history_md"}  # no freshness: no Garmin data involved
    ev = svc.get_events()
    assert ev["events"][0]["name"] == "Test Half" and ev["events"][0]["days_to_go"] == 11
    assert svc.get_context_pack("last-7d")["name"] == "7d"
    assert "# Ultimi 7 giorni" in svc.get_context_pack("7d")["markdown"]


def test_input_validation(warmed) -> None:  # type: ignore[no-untyped-def]
    svc, _ = warmed
    assert "YYYY-MM-DD" in svc.get_sleep(date_="yesterday")["error"]
    assert "future" in svc.get_hrv(date_="2030-01-01")["error"]
    assert "too long" in svc.get_range_summary(start="2026-01-01", end="2026-10-28")["error"]
    assert "after end" in svc.get_training_load(start="2026-10-28", end="2026-10-01")["error"]
    assert "unknown pack" in svc.get_context_pack("everything")["error"]
    assert "limit" in svc.list_activities(limit=0)["error"]
    assert "max_age_minutes" in svc.get_daily_brief(max_age_minutes=-1)["error"]
    assert "sections" in svc.get_athlete_profile(["secrets"])["error"]


def test_payload_size_limit(warmed, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    svc, _ = warmed
    monkeypatch.setattr(tools_mod, "MAX_PAYLOAD_CHARS", 2000)
    r = svc.get_range_summary(start="2026-08-01", end="2026-10-28")
    assert "too large" in r["error"] and "freshness" in r


def test_missing_key_is_reported_not_raised(warmed, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    svc, _ = warmed
    monkeypatch.setattr(tools_mod, "key_file_status", lambda: _key("missing"))
    r = svc.get_events()
    assert "garmin-coach doctor" in r["error"] and r["events"]


def test_append_coach_note(warmed, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    svc, _ = warmed
    notes = svc.app.paths.config_dir / "coach-notes.md"
    notes.write_text("- 2026-10-28 Lunghi nel weekend.", encoding="utf-8")  # no trailing newline
    r = svc.append_coach_note("  Long run on Saturdays,\n recovery ride on Sundays. ")
    assert r["appended"] == "- 2026-10-28 Long run on Saturdays, recovery ride on Sundays."
    secret = "GCDC" + "_abcdefghijkl"
    svc.append_coach_note(f"key {secret} must not land in the notes")
    text = notes.read_text(encoding="utf-8")
    assert text.splitlines()[1] == r["appended"] and secret not in text and text.endswith("\n")
    assert "empty" in svc.append_coach_note("  ")["error"]
    assert "too long" in svc.append_coach_note("x" * 600)["error"]


def test_refresh_rate_limit_and_cancel(warmed, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    svc, _ = warmed
    r = svc.refresh(["a", "B"], force=True)
    assert r["job_id"] and r["tiers"] == "A,B" and r["skipped"] == {}
    wait_idle(svc)
    job = jobs_mod.get_job(svc.app.jobs.conn, r["job_id"])
    assert job is not None and job["status"] == "done" and job["force"] == 1
    again = svc.refresh(["A", "B"], force=True)
    assert again["job_id"] is None and set(again["skipped"]) == {"A", "B"}
    clock.advance(minutes=6)
    assert svc.refresh(["A"], force=True)["job_id"]
    wait_idle(svc)
    assert "tiers are letters" in svc.refresh(["Z"])["error"]
    assert "scope='history'" in svc.refresh(["H"])["error"]
    assert "no job" in svc.cancel_job(9999)["error"]
    conn = db.connect(svc.app.paths.db)
    repo.set_cooldown(conn, ["C"], clock() + timedelta(hours=1), "429s")
    conn.close()
    assert "cooldown" in svc.refresh(["C"])["skipped"]["C"]


# -- server registration, resources, prompts ---------------------------------------------------------------------
EXPECTED_TOOLS = {
    "get_daily_brief", "get_athlete_profile", "get_events", "get_context_pack", "get_range_summary",
    "get_training_load", "list_activities", "get_activity", "get_sleep", "get_hrv", "get_body_battery",
    "get_heart_rate", "get_weight", "get_hr_zones", "append_coach_note", "get_sync_status", "refresh", "cancel_job",
    "get_coaching_state", "update_coaching_state",
}


def test_server_registers_everything_and_answers(warmed) -> None:  # type: ignore[no-untyped-def]
    from mcp import Client

    svc, _ = warmed
    server = build_server(svc.app)

    async def main() -> None:
        async with Client(server) as client:
            listed = {t.name: t for t in (await client.list_tools()).tools}
            tools = set(listed)
            assert tools == EXPECTED_TOOLS
            props = listed["get_activity"].input_schema["properties"]
            assert {"include_laps", "include_series", "include_splits"} <= set(props)
            assert "fit" in listed["refresh"].input_schema["properties"]["scope"]["enum"]
            res = await client.call_tool("get_daily_brief", {})
            assert not res.is_error
            body = json.loads(res.content[0].text)  # type: ignore[union-attr]
            assert body["date"] == "2026-10-28" and "freshness" in body
            res = await client.call_tool("list_activities", {"limit": 2, "type": "run"})
            assert json.loads(res.content[0].text)["count"] == 2  # type: ignore[union-attr]
            uris = {str(r.uri) for r in (await client.list_resources()).resources}
            assert uris == {f"garmin-coach://context/{n}" for n in ("daily-brief", "last-7d", "last-28d", "athlete",
                                                                     "events")} | {
                "garmin-coach://knowledge/history", "garmin-coach://knowledge/coach-notes",
                "garmin-coach://knowledge/coaching-state", "garmin-coach://status"}
            text = (await client.read_resource("garmin-coach://context/daily-brief")).contents[0].text  # type: ignore[union-attr]
            assert text.startswith("# Brief del giorno")
            assert body["coaching_state"]["state"] is None
            res = await client.call_tool("update_coaching_state", {"plan": {"from": "2026-10-29", "days": [
                {"date": "2026-10-29", "session": "corsa facile 40'"}]}})
            upd = json.loads(res.content[0].text)  # type: ignore[union-attr]
            assert upd["changed"] == ["plan"] and upd["state"]["plan"]["from"] == "2026-10-29"
            res = await client.call_tool("get_coaching_state", {})
            assert json.loads(res.content[0].text)["state"]["plan"]["to"] == "2026-10-29"  # type: ignore[union-attr]
            yaml_text = (await client.read_resource("garmin-coach://knowledge/coaching-state")).contents[0].text  # type: ignore[union-attr]
            assert yaml_text == (svc.app.paths.config_dir / STATE_FILE).read_text(encoding="utf-8")
            status = json.loads((await client.read_resource("garmin-coach://status")).contents[0].text)  # type: ignore[union-attr]
            assert status["history_loaded"] is True
            assert {p.name for p in (await client.list_prompts()).prompts} == {
                "daily_checkin", "weekly_review", "plan_week", "race_readiness", "explain_metric"}
            msg = (await client.get_prompt("daily_checkin")).messages[0].content.text  # type: ignore[union-attr]
            assert "check-in" in msg and "# Brief del giorno" in msg and "# Profilo atleta" in msg
            msg = (await client.get_prompt("race_readiness", {"event": "70.3"})).messages[0].content.text  # type: ignore[union-attr]
            assert "Test 70.3" in msg
            msg = (await client.get_prompt("explain_metric", {"name": "TSB"})).messages[0].content.text  # type: ignore[union-attr]
            assert "### ATL, CTL, TSB" in msg

    anyio.run(main)


def test_metric_section_lookup() -> None:
    assert prompts_mod.metric_section("acwr", REPO / "docs" / "METRICS.md").startswith("### ACWR")
    assert prompts_mod.metric_section("Prontezza", REPO / "docs" / "METRICS.md").startswith("### Readiness")
    assert prompts_mod.metric_section("nonsense", REPO / "docs" / "METRICS.md") is None


def test_server_boots_fast_with_empty_cache_and_no_key(tmp_path: Path) -> None:
    """A real `garmin-coach mcp` subprocess over stdio: no key file, empty cache, no network (PLAN §9.1)."""
    from mcp import Client, StdioServerParameters

    home = tmp_path / "user"
    home.mkdir()
    env = {"GARMIN_COACH_HOME": str(tmp_path / "data"), "USERPROFILE": str(home), "HOME": str(home),
           "GARMIN_PERSONAL_API_KEY": ""}
    params = StdioServerParameters(command=sys.executable, args=["-m", "garmin_coach", "mcp"], env=env,
                                   cwd=str(tmp_path))
    timings: dict[str, float] = {}

    async def main() -> None:
        t0 = time.perf_counter()
        with anyio.fail_after(60):
            await session(t0)

    async def session(t0: float) -> None:
        async with Client(params) as client:
            timings["init"] = time.perf_counter() - t0
            st = json.loads((await client.call_tool("get_sync_status", {})).content[0].text)  # type: ignore[union-attr]
            assert st["history_loaded"] is False
            body = json.loads((await client.call_tool("get_events", {})).content[0].text)  # type: ignore[union-attr]
            assert "garmin-coach doctor" in body["error"] and body["events"] == []

    anyio.run(main)
    assert timings["init"] < 6, timings  # PLAN target < 2 s; generous bound for loaded CI machines


@pytest.mark.skipif(os.name != "nt", reason="Windows-only console check")
def test_mcp_entry_point_writes_nothing_to_stdout_before_protocol(tmp_path: Path) -> None:
    """stdout belongs to the JSON-RPC stream: the CLI must not print banners there."""
    env = {**os.environ, "GARMIN_COACH_HOME": str(tmp_path / "data"), "USERPROFILE": str(tmp_path),
           "HOME": str(tmp_path)}
    env.pop("GARMIN_PERSONAL_API_KEY", None)
    p = subprocess.run([sys.executable, "-m", "garmin_coach", "mcp"], input="", capture_output=True, text=True,
                       env=env, timeout=60)
    assert p.stdout.strip() == ""


# -- coaching state (local file only) ----------------------------------------------------------------------------
SYMPTOMS = [{"text": "fastidio al polpaccio destro", "since": "2026-10-26"}]
PLAN = {"from": "2026-10-29", "days": [{"date": "2026-10-29", "session": "corsa facile 40' in Z2"},
                                       {"date": "2026-11-01", "session": "lungo 80' solo senza dolore"}],
        "change_if": ["dolore sopra 3/10: passare alla bici"]}
DECISIONS = {"open": [{"question": "gara di prova, sì o no", "decide_by": "2026-11-04"}],
             "taken": [{"text": "alla mezza con le scarpe da allenamento"}]}


@pytest.fixture
def svc(paths: Paths, settings: Settings, clock: FakeClock) -> CoachService:
    """A service without a warmed cache: the state tools never touch the database or Garmin."""
    return CoachService(make_app(paths, settings, clock, Fetchers()))


def _state_bytes(svc: CoachService) -> bytes:
    return (svc.app.paths.config_dir / STATE_FILE).read_bytes()


def test_update_replaces_only_the_sections_passed(svc: CoachService) -> None:
    r = svc.update_coaching_state(symptoms=SYMPTOMS, plan=PLAN, decisions=DECISIONS)
    assert "error" not in r, r
    assert r["changed"] == ["symptoms", "plan", "decisions"] and r["updated_by"] == "coach" and r["age_days"] == 0
    r = svc.update_coaching_state(symptoms=[{"text": "polpaccio a posto, solo rigidità al mattino"}])
    assert r["changed"] == ["symptoms"]
    st = r["state"]
    assert [s["text"] for s in st["symptoms"]] == ["polpaccio a posto, solo rigidità al mattino"]
    assert st["symptoms"][0]["noted"] == "2026-10-28"  # default: the write day
    assert len(st["plan"]["days"]) == 2 and st["plan"]["to"] == "2026-11-01"
    assert st["decisions"]["taken"][0]["date"] == "2026-10-28"
    r = svc.update_coaching_state(plan={})
    assert r["changed"] == ["plan"] and r["state"]["plan"] is None
    assert r["state"]["symptoms"] and r["state"]["decisions"]["open"]
    r = svc.update_coaching_state(symptoms=[], decisions={})
    assert r["state"] == {"symptoms": [], "plan": None, "decisions": {"open": [], "taken": []}}


def test_confirm_bumps_updated_only(svc: CoachService, clock: FakeClock) -> None:
    assert svc.update_coaching_state(confirm=True)["error"] == "no coaching state to confirm"
    first = svc.update_coaching_state(symptoms=SYMPTOMS, plan=PLAN)
    clock.advance(days=8)
    stale = svc.get_coaching_state()
    assert stale["stale"] and stale["age_days"] == 8 and stale["plan_expired"]
    r = svc.update_coaching_state(confirm=True)
    assert r["changed"] == ["confirmed"] and r["state"] == first["state"]
    assert r["updated"] > first["updated"] and r["age_days"] == 0 and not r["stale"]


def test_unreadable_file_refuses_partial_update(svc: CoachService) -> None:
    f = svc.app.paths.config_dir / STATE_FILE
    f.write_text("symptoms: [unclosed", encoding="utf-8")
    before = _state_bytes(svc)
    r = svc.update_coaching_state(symptoms=SYMPTOMS)
    assert r["error"].startswith(f"{STATE_FILE} cannot be read (") and "pass all three sections" in r["error"]
    assert _state_bytes(svc) == before
    assert "cannot be read" in svc.update_coaching_state(confirm=True)["error"]
    g = svc.get_coaching_state()
    assert g["state"] is None and g["problems"] and g["file"] == STATE_FILE
    r = svc.update_coaching_state(symptoms=SYMPTOMS, plan=PLAN, decisions=DECISIONS)
    assert "error" not in r and state_m.load_state(svc.app.paths)[1] == []


def test_update_errors_leave_the_file_untouched(svc: CoachService) -> None:
    svc.update_coaching_state(symptoms=SYMPTOMS, plan=PLAN, decisions=DECISIONS)
    before = _state_bytes(svc)
    cases: list[tuple[dict[str, Any], str]] = [
        ({}, "nothing to update: pass at least one section or confirm=true"),
        ({"symptoms": [{"text": f"sintomo {i}"} for i in range(9)]}, "symptoms: 9 items, max 8: drop resolved ones"),
        ({"plan": {"days": [{"date": "2026-10-29", "session": "x" * 412}]}},
         "plan.days[0].session is too long (412 characters, max 300): summarise it"),
        ({"decisions": {"open": [{"question": "gara sì o no", "decide_by": "2027-03-01"}]}},
         "decisions.open[0].decide_by must be within 90 days from today"),
        ({"plan": {"days": [{"date": "29/10", "session": "corsa facile"}]}},
         "plan.days[0].date must be a date as YYYY-MM-DD"),
        ({"symptoms": [{"since": "2026-10-20"}]}, "symptoms[0].text is required"),
        ({"symptoms": [{"text": "s" * 300}] * 8, "plan": {"days": [{"date": "2026-10-29", "session": "p" * 300}] * 6}},
         "state too large ("),
    ]
    for kwargs, message in cases:
        r = svc.update_coaching_state(**kwargs)
        assert set(r) == {"error"} and r["error"].startswith(message), (r, message)
        assert _state_bytes(svc) == before


def test_concurrent_updates_both_land(svc: CoachService, monkeypatch: pytest.MonkeyPatch) -> None:
    real_load = state_m.load_state

    def slow_load(*args: Any, **kwargs: Any) -> Any:
        out = real_load(*args, **kwargs)
        time.sleep(0.05)  # widen the read-modify-write window: without the lock the second write drops the first
        return out

    monkeypatch.setattr(state_m, "load_state", slow_load)
    threads = [threading.Thread(target=svc.update_coaching_state, kwargs={"symptoms": SYMPTOMS}),
               threading.Thread(target=svc.update_coaching_state, kwargs={"decisions": DECISIONS})]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st = svc.get_coaching_state()["state"]
    assert st["symptoms"] and st["decisions"]["open"]


def test_get_coaching_state_and_resource(svc: CoachService) -> None:
    assert svc.get_coaching_state() == {"state": None, "note": "no coaching state recorded yet", "file": STATE_FILE}
    assert "not found" in svc.knowledge_text("coaching-state")
    write_state(svc.app.paths)
    g = svc.get_coaching_state()
    assert set(g) == {"state", "updated", "updated_by", "age_days", "stale", "stale_after_days", "plan_expired",
                      "decisions_due", "file"}
    assert g["updated_by"] == "coach" and g["age_days"] == 0 and g["stale_after_days"] == 7
    assert g["state"]["plan"]["days"][1]["session"] == "lungo 80' solo se il polpaccio è a zero"
    assert svc.knowledge_text("coaching-state") == STATE


def test_daily_brief_carries_the_state_but_not_for_past_days(warmed) -> None:  # type: ignore[no-untyped-def]
    svc, _ = warmed
    write_state(svc.app.paths)
    brief = svc.get_daily_brief()
    assert brief["coaching_state"]["state"]["symptoms"][0]["since"] == "2026-10-26"
    assert not brief["coaching_state"]["stale"]
    past = svc.get_daily_brief(date_="2026-10-20")
    assert "error" not in past and "coaching_state" not in past
