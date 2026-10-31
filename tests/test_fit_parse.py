"""The FIT parser against synthetic files built with the SDK's Encoder (docs/FIT.md, PLAN-FIT §5)."""

from __future__ import annotations

import json
import random
from typing import Any

import fitgen
import pytest
from fitgen import Spec, build_fit

from garmin_coach.fit import parse


def ok(data: bytes, **kw: Any) -> dict[str, Any]:
    res = parse.parse_archive(fitgen.activity_zip(1, data), **kw)
    assert res.status == "ok", res.error
    assert res.summary is not None
    return res.summary


def nulls(obj: Any, path: str = "") -> list[str]:
    if obj is None:
        return [path]
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in nulls(v, f"{path}.{k}")]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in nulls(v, f"{path}[{i}]")]
    return []


def test_full_summary_of_the_interval_ride() -> None:
    res = parse.parse_archive(fitgen.activity_zip(1, fitgen.default_fit()))
    assert (res.status, res.sport, res.sub_sport, res.device) == ("ok", "cycling", "road", "fenix7")
    assert (res.n_sessions, res.n_laps, res.n_records) == (1, 11, 1870)
    s = res.summary
    assert s is not None
    meta = s["meta"]
    assert meta["parser_version"] == parse.PARSER_VERSION and meta["sdk_version"] == "21.218.0"
    assert meta["file_type"] == "activity" and meta["time_created"] == "2026-10-27T06:00:00Z"
    assert meta["laps_truncated"] is False and meta["truncated"] is False and meta["errors"] == []
    assert s["session"] == {
        "sport": "cycling", "sub_sport": "road", "profile_name": "Road", "start_time": "2026-10-27T06:00:00Z",
        "end_time": "2026-10-27T06:31:10Z", "elapsed_s": 1870.0, "timer_s": 1870.0, "paused_s": 0.0,
        "distance_m": 15300.0, "ascent_m": 19, "descent_m": 2, "calories_kcal": 650, "avg_speed_mps": 8.18,
        "max_speed_mps": 10.0, "avg_hr": 140, "max_hr": 155, "min_hr": 118, "avg_cadence": 85, "max_cadence": 90,
        "avg_power": 209, "max_power": 300, "normalized_power": 235, "ftp": 250, "tss": 52.3,
        "intensity_factor": 0.94, "work_kj": 391, "aerobic_te": 3.4, "anaerobic_te": 1.8, "rpe": 7.0, "num_laps": 11,
    }
    assert "sessions" not in s
    assert s["zones"] == {
        "source": "time_in_zone",
        "hr": {"time_s": [0, 610, 300, 520, 440, 0], "boundaries_bpm": fitgen.TIZ_HR_BOUNDS,
               "calc_type": "percent_max_hr", "max_hr": 185, "resting_hr": 48, "threshold_hr": 166},
        "power": {"time_s": [100, 200, 300, 400, 500, 300, 70], "boundaries_w": fitgen.TIZ_POWER_BOUNDS, "ftp": 250,
                  "calc_type": "percent_ftp"},
    }
    laps = s["laps"]
    assert [x["i"] for x in laps] == list(range(1, 12))
    assert laps[0] == {
        "i": 1, "start_time": "2026-10-27T06:00:00Z", "elapsed_s": 170.0, "timer_s": 170.0, "distance_m": 1700.0,
        "avg_speed_mps": 10.0, "avg_hr": 150, "max_hr": 150, "avg_power": 300, "max_power": 300, "avg_cadence": 90,
        "intensity": "active", "trigger": "manual", "wkt_step_index": 0, "balance_right_pct": 50.8,
    }
    assert laps[1]["intensity"] == "rest" and laps[1]["avg_power"] == 100 and laps[10]["avg_hr"] == 155
    assert s["intervals"] == {
        "work": {"n": 6, "avg_duration_s": 170.0, "avg_distance_m": 1700.0, "avg_hr": 152, "max_hr": 155,
                 "avg_power": 300, "avg_speed_mps": 10.0, "first_to_last_speed_pct": 0.0, "first_to_last_hr": 5},
        "rest": {"n": 5, "avg_duration_s": 170.0, "avg_hr": 120, "avg_speed_mps": 6.0},
        "lap_indexes": {"work": [1, 3, 5, 7, 9, 11], "rest": [2, 4, 6, 8, 10]},
    }
    assert s["dynamics"] == {"bike": {"balance_right_pct": 51.2, "torque_effectiveness_pct": [78.0, 79.5],
                                      "pedal_smoothness_pct": [21.0, 22.5], "pco_mm": [2, -1]}}
    rec = s["records"]
    assert rec["n"] == 1870 and rec["interval_s"] == 1 and rec["power_source"] == "record.power"
    assert rec["coverage_pct"] == {"hr": 100.0, "power": 100.0, "cadence": 100.0, "position": 100.0}
    assert rec["power_zero_pct"] == 0.0
    assert s["route"] == {"start": [45.4379, 12.3351], "end": [45.4566, 12.3538]}  # 4 decimals (~10 m)
    assert s["device"] == {"manufacturer": "garmin", "product": "fenix7", "software_version": 16.22,
                           "sensors": ["heart_rate"]}


def test_privacy_no_serials_no_nulls_and_small() -> None:
    s = ok(fitgen.default_fit())
    text = json.dumps(s)
    assert str(fitgen.SERIAL) not in text and str(fitgen.SERIAL + 1) not in text and "serial" not in text
    assert nulls(s) == []
    assert len(text) < 20_000  # PLAN-FIT D-075 target per activity
    assert "record_mesgs" not in text and text.count("45.43") <= 1  # no track: start point only


def test_laps_cap_and_flag() -> None:
    s = ok(fitgen.default_fit(), max_laps=4)
    assert len(s["laps"]) == 4 and s["meta"]["laps_truncated"] is True and s["meta"]["n_laps"] == 11
    assert s["intervals"]["work"]["n"] == 6  # intervals use every lap


def test_intervals_need_two_work_laps_and_a_rest_lap() -> None:
    assert "intervals" not in ok(build_fit(Spec(laps=1, lap_s=30)))
    assert "intervals" not in ok(build_fit(Spec(laps=2, lap_s=30)))  # one work + one rest
    three = ok(build_fit(Spec(laps=3, lap_s=30)))["intervals"]
    assert three["work"]["n"] == 2 and three["rest"]["n"] == 1
    fade = ok(build_fit(Spec(laps=3, lap_s=30, speed=lambda t, w, rep: (10.0 - rep) if w else 5.0)))["intervals"]
    assert fade["work"]["first_to_last_speed_pct"] == -10.0 and fade["work"]["first_to_last_hr"] == 1


def test_decoupling_from_halves_with_known_ratios() -> None:
    # Constant 200 W; HR 100 in the first half of the timer time, 105 in the second: (2.0 - 1.9048) / 2.0 = 4.8 %
    spec = Spec(laps=1, lap_s=2000, hr=lambda t, w, r: 100 if t < 1000 else 105, power=lambda t, w, r: 200)
    assert ok(build_fit(spec))["records"]["decoupling_pct"] == 4.8
    # Running: Pa:HR on speed, no power
    run = Spec(sport="running", sub_sport="generic", laps=1, lap_s=2000, power=lambda t, w, r: None,
               hr=lambda t, w, r: 150, speed=lambda t, w, r: 4.0 if t < 1000 else 3.8)
    rec = ok(build_fit(run))["records"]
    assert rec["decoupling_pct"] == 5.0 and "power_source" not in rec and "power_zero_pct" not in rec
    # Under 30 minutes of timer time: no decoupling
    assert "decoupling_pct" not in ok(build_fit(Spec(laps=1, lap_s=1700)))["records"]


def test_pause_is_excluded_from_timeline_and_series() -> None:
    spec = Spec(laps=1, lap_s=2000, pause=(900, 1500), power=lambda t, w, r: 200,
                hr=lambda t, w, r: 100 if t < 1000 else 105)
    s = ok(build_fit(spec))
    assert s["session"]["elapsed_s"] == 2600.0 and s["session"]["timer_s"] == 2000.0 and s["session"]["paused_s"] == 600.0
    assert s["records"]["decoupling_pct"] == 4.8  # halves split by timer time, not by wall clock
    starts = [b["t_s"] for b in s["series"]]
    assert all(not 900 <= t < 1500 for t in starts)  # empty buckets (the pause) are skipped
    assert len(starts) <= 48


def test_series_buckets() -> None:
    s = ok(fitgen.default_fit())
    series = s["series"]
    assert len(series) == 32 and [b["t_s"] for b in series] == [60 * i for i in range(32)]  # bucket_s = 60
    assert series[0] == {"t_s": 0, "hr": 150, "power": 300, "speed_mps": 10.0, "cadence": 90, "alt_m": 101,
                         "dist_m": 600}
    coarse = ok(fitgen.default_fit(), series_points=10)["series"]
    assert len(coarse) == 10 and coarse[1]["t_s"] == 187  # ceil(1870 / 10)


def test_route_omitted_without_positions() -> None:
    s = ok(build_fit(Spec(laps=3, lap_s=20, positions=False)))
    assert "route" not in s and s["records"]["coverage_pct"]["position"] == 0.0


def test_zone_sources() -> None:
    floors = [110, 135, 150, 160, 170]
    computed = ok(build_fit(Spec(time_in_zone=False)), hr_zone_floors=floors)["zones"]
    # rest laps at 120 bpm -> zone 1 (index 1), work laps at 150-155 bpm -> zone 3; index 0 = below zone 1
    assert computed == {"source": "computed", "hr": {"time_s": [0, 850, 0, 1020, 0, 0], "floors_bpm": floors}}
    session = ok(build_fit(Spec(time_in_zone=False, session_zones=True)), hr_zone_floors=floors)["zones"]
    assert session == {"source": "session", "hr": {"time_s": [10, 20, 30, 40, 50, 60]}}
    assert "zones" not in ok(build_fit(Spec(time_in_zone=False)))  # no floors passed: nothing to compute


def test_developer_power_field() -> None:
    rec = ok(build_fit(Spec(laps=3, lap_s=20, dev_power=True)))["records"]
    assert rec["power_source"] == "developer:Power" and rec["coverage_pct"]["power"] == 100.0


def test_running_dynamics() -> None:
    extra = {"avg_step_length": 1210.0, "avg_vertical_oscillation": 88.2, "avg_stance_time": 241.0,
             "avg_vertical_ratio": 8.4, "avg_stance_time_balance": 49.8, "avg_grade_adjusted_speed": 3.1,
             "avg_fractional_cadence": 0.5}
    s = ok(build_fit(Spec(sport="running", sub_sport="generic", laps=3, lap_s=20, extra=extra)))
    assert s["dynamics"] == {"run": {"cadence_spm": 171, "step_length_m": 1.21, "vertical_oscillation_mm": 88.2,
                                     "vertical_ratio_pct": 8.4, "stance_time_ms": 241, "stance_time_balance_pct": 49.8,
                                     "gap_mps": 3.1}}
    assert s["session"]["avg_cadence_spm"] == 171 and s["session"]["avg_gap_mps"] == 3.1
    assert s["laps"][0]["avg_cadence_spm"] == 180  # lap cadence 90 strides/min


def test_archive_errors() -> None:
    gpx = parse.parse_archive(fitgen.zip_bytes("123.gpx", fitgen.GPX))
    assert gpx.status == "no_fit" and gpx.summary is None
    garbage = parse.parse_archive(random.Random(7).randbytes(4000))
    assert garbage.status == "parse_error" and garbage.error == "not a ZIP or FIT file"
    not_fit = parse.parse_archive(fitgen.zip_bytes("1_ACTIVITY.fit", b"\x00" * 100))
    assert not_fit.status == "parse_error" and not_fit.error
    bare = parse.parse_archive(fitgen.small_fit())  # a body that is already FIT
    assert bare.status == "ok"


def test_oversized_member_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parse, "MAX_MEMBER_BYTES", 1000)
    res = parse.parse_archive(fitgen.activity_zip(1, fitgen.small_fit()))
    assert res.status == "parse_error" and res.error == "member too large"


def test_truncated_file_keeps_decoder_errors_in_meta() -> None:
    data = fitgen.default_fit()
    res = parse.parse_archive(data[:-40])  # activity message and CRC cut off, the session survives
    assert res.status == "ok" and res.summary is not None
    assert res.summary["meta"]["errors"] and len(res.summary["meta"]["errors"]) <= parse.MAX_ERRORS


def test_hard_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    full = ok(fitgen.default_fit())
    monkeypatch.setattr(parse, "MAX_JSON_CHARS", len(json.dumps(full, separators=(",", ":"))) - 100)
    s = ok(fitgen.default_fit())
    assert "series" not in s and s["meta"]["truncated"] is True and len(s["laps"]) == 11
    monkeypatch.setattr(parse, "MAX_JSON_CHARS", 1500)
    tiny = ok(fitgen.default_fit())
    assert "laps" not in tiny and tiny["meta"]["truncated"] is True
