from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from garmin_coach.athlete import Event
from garmin_coach.metrics import events, fitness, load, recovery, sport

D0 = date(2026, 1, 1)


def series(values: list[float]) -> list[tuple[date, float]]:
    return [(D0 + timedelta(days=i), v) for i, v in enumerate(values)]


# -- EWMA / ATL / CTL / TSB -----------------------------------------------------------------------------------
def test_ewma_known_values() -> None:
    k7 = 1 - math.exp(-1 / 7)
    out = load.ewma([100, 0, 0], 7)
    assert out[0] == pytest.approx(100 * k7)
    assert out[1] == pytest.approx(100 * k7 * (1 - k7))
    assert out[2] == pytest.approx(100 * k7 * (1 - k7) ** 2)


def test_constant_load_converges_and_tsb_goes_to_zero() -> None:
    pts = load.compute(series([50.0] * 400))
    last = pts[-1]
    assert last.atl == pytest.approx(50, abs=0.01) and last.ctl == pytest.approx(50, abs=0.01)
    assert last.tsb == pytest.approx(0, abs=0.01)
    assert last.acwr == pytest.approx(1.0)
    assert last.monotony is None  # zero variance: undefined, not infinite


def test_tsb_uses_previous_day() -> None:
    pts = load.compute(series([0, 100, 0]))
    assert pts[0].tsb is None
    assert pts[1].tsb == pytest.approx(pts[0].ctl - pts[0].atl)  # the big day does not affect its own TSB
    assert pts[2].tsb == pytest.approx(pts[1].ctl - pts[1].atl) and pts[2].tsb < 0


def test_missing_days_are_rest_days() -> None:
    s = load.daily_series({D0: 100, D0 + timedelta(days=3): 50}, D0, D0 + timedelta(days=4))
    assert [v for _, v in s] == [100, 0, 0, 50, 0]


# -- ACWR edge cases ------------------------------------------------------------------------------------------
def test_acwr_needs_28_days_and_nonzero_chronic() -> None:
    pts = load.compute(series([0.0] * 40))
    assert all(p.acwr is None for p in pts)  # chronic = 0 -> undefined
    pts = load.compute(series([10.0] * 27))
    assert all(p.acwr is None for p in pts)  # less than 28 days of history


def test_acwr_spike_and_drop_flags() -> None:
    spike = load.compute(series([20.0] * 21 + [80.0] * 7))[-1]
    assert spike.acwr == pytest.approx(560 / ((20 * 21 + 560) / 4))
    assert load.acwr_flag(spike.acwr) == "alto"
    drop = load.compute(series([50.0] * 21 + [0.0] * 7))[-1]
    assert drop.acwr == 0 and load.acwr_flag(drop.acwr) == "basso"
    assert load.acwr_flag(1.1) is None and load.acwr_flag(None) is None


def test_monotony_and_strain_foster() -> None:
    week = [100, 0, 100, 0, 100, 0, 100]
    p = load.compute(series(week))[-1]
    mean, sd = sum(week) / 7, (sum((x - 400 / 7) ** 2 for x in week) / 7) ** 0.5
    assert p.monotony == pytest.approx(mean / sd)
    assert p.strain == pytest.approx(400 * mean / sd)


# -- TRIMP fallback -------------------------------------------------------------------------------------------
def test_trimp_banister_and_guards() -> None:
    t = load.trimp(3600, 150, 50, 190, "M")
    hrr = 100 / 140
    assert t == pytest.approx(60 * hrr * 0.64 * math.exp(1.92 * hrr))
    assert load.trimp(3600, 150, 50, 190, "F") != t
    assert load.trimp(None, 150, 50, 190) is None
    assert load.trimp(3600, None, 50, 190) is None
    assert load.trimp(3600, 150, 60, 55) is None  # max <= rest: implausible
    assert load.trimp(3600, 40, 50, 190) == 0.0  # below resting HR


def test_calibration_factor() -> None:
    assert load.calibration_factor([(110, 100)] * 9) == 1.0  # too few samples
    assert load.calibration_factor([(110, 100)] * 12) == pytest.approx(1.1)
    assert load.calibration_factor([(1000, 1)] * 12) == 3.0  # clamped


# -- recovery / readiness -------------------------------------------------------------------------------------
def hrv_rows(values: list[int | None], low: int = 70, high: int = 90) -> list[dict]:
    return [{"date": (D0 + timedelta(days=i)).isoformat(), "last_night_avg": v, "weekly_avg": 80,
             "baseline_balanced_low": low, "baseline_balanced_upper": high, "status": "BALANCED"}
            for i, v in enumerate(values)]


def test_hrv_view_nights_below_and_slope() -> None:
    v = recovery.hrv_view(hrv_rows([80, 85, 75, 68, 66, 65]))
    assert v is not None and v.nights_below == 3 and v.slope_7d is not None and v.slope_7d < 0
    v2 = recovery.hrv_view(hrv_rows([65, 80]))
    assert v2 is not None and v2.nights_below == 0
    assert recovery.hrv_view([]) is None


def test_readiness_full_and_partial() -> None:
    hv = recovery.hrv_view(hrv_rows([80, 82, 81]))
    full = recovery.readiness(hv, 90, 95, -1, 5.0, 80.0)
    assert full.score is not None and full.score >= 90 and not full.missing
    bad = recovery.readiness(recovery.hrv_view(hrv_rows([60, 55, 50])), 45, 30, 7, -50.0, 80.0)
    assert bad.score is not None and bad.score < 40
    assert any("FC a riposo +7" in r for r in bad.reasons)
    partial = recovery.readiness(None, 80, None, None, None, None)
    assert partial.score is None and set(partial.missing) == {"HRV", "Body Battery", "FC a riposo", "forma"}


def test_readiness_rescales_missing_parts() -> None:
    hv = recovery.hrv_view(hrv_rows([80, 82, 81]))
    r = recovery.readiness(hv, 80, None, 0, None, None)  # HRV 30 + sleep 25 + RHR 15 = 70 of 100 weight
    assert r.score == round((30 + 0.8 * 25 + 15) / 70 * 100)
    assert recovery.readiness_label(r.score) in {"alta", "buona"}
    assert recovery.readiness_label(None) == "non calcolabile"


def test_rhr_delta() -> None:
    assert recovery.rhr_delta(45, 40.4) == 5
    assert recovery.rhr_delta(None, 40) is None


# -- events -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("days", "phase"), [(0, "giorno gara"), (3, "settimana gara"), (10, "scarico"),
                                             (20, "picco"), (60, "costruzione"), (84, "base"), (177, "base")])
def test_phase_by_weeks_out(days: int, phase: str) -> None:
    assert events.phase(days)[0] == phase


def test_event_views_skip_past_and_sort() -> None:
    today = date(2026, 10, 29)
    evs = [Event(name="past", date=date(2026, 10, 25)), Event(name="next", date=date(2026, 11, 1))]
    views = events.event_views(evs, today)
    assert [v.event.name for v in views] == ["next"] and views[0].days_to == 3


# -- fitness proxies ------------------------------------------------------------------------------------------
def act(day: str, type_key: str, **kw: object) -> dict:
    return {"activity_id": hash((day, type_key)) % 10**8, "start_local": f"{day}T08:00:00", "type_key": type_key,
            "duration_s": 3600, **kw}


def test_efficiency_splits_indoor_outdoor_and_periods() -> None:
    today = date(2026, 10, 29)
    acts = [
        act("2026-10-20", "running", avg_hr=140, avg_speed_mps=3.5),
        act("2026-09-01", "running", avg_hr=140, avg_speed_mps=3.4),
        act("2026-10-21", "indoor_cycling", avg_hr=120, norm_power=180),
        act("2026-10-22", "running", avg_hr=165, avg_speed_mps=4.0),  # too hard: not "steady"
    ]
    eff = {e.label: e for e in fitness.efficiency(acts, today, lthr_run=170, lthr_bike=160)}
    run = eff["corsa all'aperto, metri per battito"]
    assert run.recent == pytest.approx(3.5 * 60 / 140) and run.previous == pytest.approx(3.4 * 60 / 140)
    assert run.n_recent == 1 and run.change_pct is not None and run.change_pct > 0
    assert "bici indoor, watt per bpm" in eff and "bici all'aperto, watt per bpm" not in eff


def test_power_bests_kept_apart() -> None:
    today = date(2026, 10, 29)
    acts = [act("2026-10-01", "road_biking", pwr_300s=250, pwr_60s=350),
            act("2026-10-02", "indoor_cycling", pwr_300s=260),
            act("2026-05-01", "road_biking", pwr_300s=400)]  # older than 90 days
    b = fitness.power_bests(acts, today)
    assert b == {"outdoor": {60: 350, 300: 250}, "indoor": {300: 260}}


def test_zone_distribution() -> None:
    z = fitness.zone_distribution([{"hr_z1_s": 30, "hr_z2_s": 70}, {"hr_z3_s": 100}])
    assert z == {1: 0.15, 2: 0.35, 3: 0.5, 4: 0.0, 5: 0.0}
    assert fitness.zone_distribution([{}]) is None


def test_sport_families() -> None:
    assert sport.family("treadmill_running") == sport.RUN and sport.is_indoor("treadmill_running")
    assert sport.family("virtual_ride") == sport.BIKE and sport.is_indoor("virtual_ride")
    assert sport.family("open_water_swimming") == sport.SWIM
    assert sport.family("multi_sport") == sport.MULTI and sport.family("walking") == sport.OTHER
