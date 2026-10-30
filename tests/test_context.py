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
from garmin_coach.settings import STATE_FILE, Paths, Settings
from garmin_coach.state import CoachingState, save_state
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
# Placeholder coaching state as of the fake clock's day (2026-10-28), written by hand with an explicit `updated`.
STATE = """updated: 2026-10-28T09:30:00+01:00
updated_by: coach
symptoms:
  - {text: "fastidio al polpaccio destro, in calo", since: 2026-10-26, noted: 2026-10-28}
plan:
  from: 2026-10-29
  to: 2026-11-04
  days:
    - {date: 2026-10-29, session: corsa facile 40' in Z2}
    - {date: 2026-11-01, session: lungo 80' solo se il polpaccio è a zero}
  change_if: ["dolore sopra 3/10 in corsa: passare alla bici per due giorni"]
decisions:
  open:
    - {question: "gara di prova (8 nov), sì o no", decide_by: 2026-11-04, noted: 2026-10-28}
  taken:
    - {text: alla mezza con le scarpe da allenamento, date: 2026-10-27}
"""
STALE_STATE = """updated: 2026-10-19T20:00:00+02:00
updated_by: coach
symptoms:
  - {text: fastidio al polpaccio destro, since: 2026-10-17}
plan:
  days:
    - {date: 2026-10-25, session: lungo 70'}
decisions:
  open:
    - {question: "gara di prova, sì o no", decide_by: 2026-10-28}
    - {question: "scarpe nuove, quali", decide_by: 2026-10-26}
"""


def write_state(paths: Paths, text: str = STATE) -> None:
    (paths.config_dir / STATE_FILE).write_text(text, encoding="utf-8")


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
    assert "## Stato di coaching\n_Nessuno stato di coaching registrato._" in daily
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
    """Snapshot of the daily brief from the fixture DB with a coaching state (the 'Generato' line depends on the
    local time zone)."""
    write_state(paths)
    _, texts = render.build_and_render(full, paths, settings, clock=clock, write=False, names=("daily-brief",))
    body = "\n".join(line for line in texts["daily-brief"].splitlines() if not line.startswith("_Generato"))
    golden = GOLDEN / "daily-brief.md"
    if os.environ.get("UPDATE_GOLDEN") == "1" or not golden.exists():
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(body + "\n", encoding="utf-8")
    assert body + "\n" == golden.read_text(encoding="utf-8")
    assert body.index("## Segnali") < body.index("## Stato di coaching (aggiornato il 28/10, oggi)") < body.index(
        "## Prontezza")
    assert "- dal 26/10: fastidio al polpaccio destro, in calo" in body and "**Piano 29/10–04/11:**" in body
    assert "(entro il 04/11)" in body and "**Decisioni prese:** 27/10 alla mezza" in body


def test_stale_state_heading_and_flags(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    write_state(paths, STALE_STATE)
    pack, texts = render.build_and_render(full, paths, settings, clock=clock, write=False, names=("daily-brief",))
    daily = texts["daily-brief"]
    assert "## Stato di coaching (aggiornato il 19/10, 9 giorni fa: da confermare)" in daily
    assert "**Piano 25/10–25/10:**" in daily  # no from/to written by hand: the plan days give the window
    flags = pack.flags
    assert "Stato di coaching vecchio di 9 giorni: confermare sintomi, piano e decisioni prima di usarli" in flags
    assert "Piano scaduto il 25/10: serve un nuovo piano" in flags
    assert "Decisione in sospeso entro oggi: gara di prova, sì o no" in flags
    assert "Decisione in sospeso scaduta il 26/10: scarpe nuove, quali" in flags
    assert pack.coaching_state and pack.coaching_state["stale"] and pack.coaching_state["age_days"] == 9


def test_unreadable_state_is_reported(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    write_state(paths, "symptoms: [unclosed")
    pack, texts = render.build_and_render(full, paths, settings, clock=clock, write=False, names=("daily-brief",))
    assert "Stato di coaching non leggibile" in texts["daily-brief"]
    assert any(f.startswith("File personali: coaching-state.yaml non valido") for f in pack.flags)


def test_full_state_fits_the_daily_budget(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    """A state near the 4 000-character file cap still keeps the daily brief within its (raised) token budget."""
    today = clock().astimezone().date()
    words = "corsa facile in Z2 con allunghi finali e controllo del dolore al polpaccio destro "
    state = CoachingState.model_validate({
        "symptoms": [{"text": f"{i} {words}" * 2, "since": today} for i in range(6)],
        "plan": {"days": [{"date": today + timedelta(days=i), "session": f"{i} {words}"} for i in range(12)],
                 "change_if": [words] * 3},
        "decisions": {"open": [{"question": words, "decide_by": today}] * 2, "taken": [{"text": words}] * 2},
    })
    save_state(paths, state, clock())
    assert len((paths.config_dir / STATE_FILE).read_text(encoding="utf-8")) > 3500
    _, texts = render.build_and_render(full, paths, settings, clock=clock, write=False, names=("daily-brief",))
    assert fmt.approx_tokens(texts["daily-brief"]) <= settings.context.daily_token_budget


def test_state_write_does_not_change_the_metrics_fingerprint(full, paths: Paths, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    inp = load_inputs(paths)
    today = clock().astimezone().date()
    assert builder.ensure_metrics(full, inp, today, builder.inputs_mtime(paths.config_dir)) is True
    before = builder.inputs_mtime(paths.config_dir)
    write_state(paths)
    later = time.time() + 120
    for name in (STATE_FILE, "history.md", "coach-notes.md"):
        os.utime(paths.config_dir / name, (later, later))
    assert builder.inputs_mtime(paths.config_dir) == before
    assert builder.ensure_metrics(full, inp, today, builder.inputs_mtime(paths.config_dir)) is False  # no recompute
    os.utime(paths.config_dir / "athlete.yaml", (later + 60, later + 60))
    assert builder.ensure_metrics(full, inp, today, builder.inputs_mtime(paths.config_dir)) is True


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
