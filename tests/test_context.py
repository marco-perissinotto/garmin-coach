from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fakes import FakeClock, FakeGarmin

from garmin_coach.athlete import load_inputs
from garmin_coach.context import builder, fmt, render
from garmin_coach.garmin.endpoints import REFRESH_TIERS, Tier
from garmin_coach.settings import Paths, Settings
from garmin_coach.sync.freshness import FreshnessGate
from garmin_coach.sync.runner import Runner

GOLDEN = Path(__file__).parent / "golden"

ATHLETE = """name: "Test Athlete"
birth_year: 1980-01-01
sex: M
height_cm: 175
weight_kg: 70
weight_date: 2026-09-09
sports: [running, cycling]
max_hr: 186
ftp: 245
constraints: ["no long runs over 25 km"]
availability: {mon: 60, sat: 180}
advice_language: it
"""
EVENTS = """events:
  - {name: "Test Half", date: 2026-11-08, sport: running, distance_km: 21.1, goal: "sub 1:35", priority: B}
  - {name: "Test 70.3", date: 2027-04-24, sport: triathlon, distance_km: 113, priority: A}
"""


def write_inputs(paths: Paths) -> None:
    (paths.config_dir / "athlete.yaml").write_text(ATHLETE, encoding="utf-8")
    (paths.config_dir / "events.yaml").write_text(EVENTS, encoding="utf-8")
    (paths.config_dir / "history.md").write_text("# Storia atletica\n- Corre dal 2018.\n", encoding="utf-8")
    (paths.config_dir / "coach-notes.md").write_text("- 2026-10-28 Lunghi nel weekend.\n", encoding="utf-8")


@pytest.fixture
def full(paths: Paths, settings: Settings, conn, clock: FakeClock):  # type: ignore[no-untyped-def]
    write_inputs(paths)
    Runner(conn, settings, FakeGarmin(), clock=clock).run({Tier.H})
    Runner(conn, settings, FakeGarmin(), clock=clock).run(set(REFRESH_TIERS))
    return conn


def test_inputs_loader_tolerates_dates_and_missing(paths: Paths) -> None:
    inp = load_inputs(paths)
    assert len(inp.problems) == 4 and inp.athlete.advice_language == "it"
    write_inputs(paths)
    inp = load_inputs(paths)
    assert inp.problems == [] and inp.athlete.birth_year == 1980 and [e.name for e in inp.events] == [
        "Test Half", "Test 70.3"]
    (paths.config_dir / "events.yaml").write_text("events: [{name: broken}]", encoding="utf-8")
    assert any("evento #1" in p for p in load_inputs(paths).problems)


def test_daily_brief_full_data(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    pack, texts = render.build_and_render(full, paths, settings, clock=clock)
    daily = texts["daily-brief"]
    assert fmt.approx_tokens(daily) <= settings.context.daily_token_budget
    for section in ("## Segnali", "## Prontezza", "## Sonno", "## Recupero", "## Attività", "## Carico", "## Prossimi"):
        assert section in daily
    # data exists -> no "no data" lines for these sections
    for missing in ("Nessun dato di sonno", "HRV: nessun dato", "Nessun dato di carico", "FC a riposo: nessun dato"):
        assert missing not in daily, missing
    assert pack.readiness and pack.readiness["score"] is not None
    assert "Test Half" in daily and "tra 11 giorni" in daily  # fake clock: 2026-10-28
    weekly = texts["last-7d"] + texts["last-28d"]
    assert fmt.approx_tokens(weekly) <= settings.context.weekly_token_budget
    assert "| Giorno |" in texts["last-28d"] and "Indicatori di forma" in texts["last-28d"]
    assert "Test Athlete" in texts["athlete"] and "Corre dal 2018" in texts["athlete"]
    assert "Lunghi nel weekend" in texts["athlete"]
    assert "get_daily_brief" in texts["coach-instructions"] and "Test Athlete" not in texts["coach-instructions"]
    assert fmt.approx_tokens(texts["coach-instructions"]) <= 450
    # files written
    for n in render.PACKS:
        assert (paths.context_dir / f"{n}.md").exists()
    data = json.loads((paths.context_dir / "context.json").read_text(encoding="utf-8"))
    assert data["today"] == "2026-10-28" and data["load"]["ctl"] > 0


def test_empty_cache_renders_explicit_no_data(paths: Paths, settings: Settings, conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    _, texts = render.build_and_render(conn, paths, settings, clock=clock, write=False, names=render.PACKS)
    daily = texts["daily-brief"]
    for line in ("Nessun dato di sonno per stanotte", "HRV: nessun dato", "FC a riposo: nessun dato",
                 "Nessun dato sufficiente", "nessuna attività registrata", "Nessun dato di carico",
                 "Nessun evento in programma", "Storico non ancora caricato"):
        assert line in daily, line
    assert "File personali:" in daily  # missing athlete.yaml etc. are reported, not hidden
    assert "nessuna attività in cache" in texts["activities-recent"]
    assert "_history.md non presente" in texts["athlete"]


def test_metrics_recomputed_only_when_data_changes(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    inp = load_inputs(paths)
    today = clock().astimezone().date()
    assert builder.ensure_metrics(full, inp, today) is True
    assert builder.ensure_metrics(full, inp, today) is False
    full.execute("UPDATE sleep SET updated_at='2099-01-01T00:00:00+00:00' WHERE date=(SELECT MAX(date) FROM sleep)")
    assert builder.ensure_metrics(full, inp, today) is True


def test_render_from_cache_is_fast(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    render.build_and_render(full, paths, settings, clock=clock)  # metrics computed once
    t0 = time.perf_counter()
    render.build_and_render(full, paths, settings, clock=clock, names=("daily-brief",), write=False)
    assert time.perf_counter() - t0 < 1.0


def test_stale_cache_refreshes_ab_then_header_shows_new_age(full, paths: Paths, settings: Settings,  # type: ignore[no-untyped-def]
                                                             clock: FakeClock) -> None:
    clock.advance(hours=3)
    stale_pack, _ = render.build_and_render(full, paths, settings, clock=clock, write=False, names=("daily-brief",))
    assert stale_pack.cache["A"] == "3 h"
    fakes: list[FakeGarmin] = []

    def factory(budget: int, timeout: float | None = None) -> FakeGarmin:
        fakes.append(FakeGarmin(budget=budget))
        return fakes[-1]

    gate = FreshnessGate(paths, settings, full, fetcher_factory=factory, clock=clock, background=False)
    fr = gate.ensure_fresh({Tier.A, Tier.B}, trigger="cli")
    assert sum(f.calls_made for f in fakes) > 0 and not fr.stale
    pack, texts = render.build_and_render(full, paths, settings, clock=clock, freshness=fr, write=False,
                                          names=("daily-brief",))
    assert pack.cache["A"] == "0 min" and pack.cache["B"] == "0 min"
    assert "età cache: A 0 min · B 0 min" in texts["daily-brief"]


def test_stale_reason_printed_in_header(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    class Stale:
        stale = True
        stale_reason = "cooldown"

    _, texts = render.build_and_render(full, paths, settings, clock=clock, freshness=Stale(), write=False,
                                       names=("daily-brief",))
    assert "⚠ Dati serviti dalla cache non aggiornata: cooldown" in texts["daily-brief"]


def test_post_run_hook_writes_packs(paths: Paths, settings: Settings, conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    write_inputs(paths)
    Runner(conn, settings, FakeGarmin(), clock=clock,
           on_finished=lambda c: render.after_sync(c, paths, settings, clock)).run({Tier.H})
    assert (paths.context_dir / "daily-brief.md").exists()
    assert conn.execute("SELECT COUNT(*) FROM metrics_daily").fetchone()[0] > 300


def test_after_sync_never_raises(paths: Paths, settings: Settings, conn, clock: FakeClock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(render, "build_and_render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    render.after_sync(conn, paths, settings, clock)  # logged, not raised


def test_flags(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    today = clock().astimezone().date().isoformat()
    full.execute("UPDATE daily SET rhr=60, rhr_7d_avg=48, bb_at_wake=30 WHERE date=?", (today,))
    full.execute("UPDATE sleep SET score=45 WHERE date=?", (today,))
    pack = builder.build(full, load_inputs(paths), clock=clock)
    flags = " | ".join(pack.flags)
    assert "Sonno scarso" in flags and "FC a riposo +12" in flags and "Body Battery al risveglio bassa" in flags


def test_golden_daily_brief(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    """Snapshot of the daily brief from the fixture DB (the 'Generato' line depends on the local time zone)."""
    _, texts = render.build_and_render(full, paths, settings, clock=clock, write=False, names=("daily-brief",))
    body = "\n".join(line for line in texts["daily-brief"].splitlines() if not line.startswith("_Generato"))
    golden = GOLDEN / "daily-brief.md"
    if os.environ.get("UPDATE_GOLDEN") == "1" or not golden.exists():
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(body + "\n", encoding="utf-8")
    assert body + "\n" == golden.read_text(encoding="utf-8")


def test_export_zip(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    import zipfile

    render.build_and_render(full, paths, settings, clock=clock)
    z = render.export_zip(paths, clock)
    names = set(zipfile.ZipFile(z).namelist())
    assert names == {f"{n}.md" for n in render.PACKS} | {"context.json"}


def test_week_table_covers_four_weeks(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    pack = builder.build(full, load_inputs(paths), clock=clock)
    assert len(pack.weeks) == 4 and pack.weeks[0]["label"].endswith("(in corso)")
    assert len(pack.days7) == 7 and len(pack.days28) == 28
    assert pack.days28[-1]["day"].endswith(f"{(clock().astimezone().date() - timedelta(days=27)):%d/%m}")
