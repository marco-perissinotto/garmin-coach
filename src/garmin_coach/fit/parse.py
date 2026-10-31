"""FIT bytes -> FitSummary dict (schema in docs/FIT.md). Pure: no database, no paths, no logging of content.

The official SDK (``garmin-fit-sdk``) is imported lazily, so offline commands and the MCP boot never load it. Every
output field is optional: a value the file does not carry is left out, never written as ``null``. Units follow the
rest of the project (m, s, m/s, bpm, W, °C); datetimes are ISO 8601 UTC strings; positions are degrees, 4 decimals.
"""

from __future__ import annotations

import bisect
import io
import json
import math
import statistics
import struct
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ..secrets import redact

PARSER_VERSION = 1

MAX_MEMBER_BYTES = 50 * 1024 * 1024  # zip-bomb guard: a real activity FIT is well below 10 MB
MAX_JSON_CHARS = 120_000  # hard cap of one summary (D-075): drop series, then cut laps
CUT_LAPS = 100
MAX_ERRORS = 5
SEMICIRCLE_DEG = 180 / 2**31
DEV_POWER_NAMES = ("power", "running power")  # developer fields used when record.power is absent
TIMER_STOPS = frozenset({"stop", "stop_all", "stop_disable", "stop_disable_all"})
MIN_DECOUPLING_TIMER_S = 1800
MIN_DECOUPLING_COVERAGE = 80.0
WORK_INTENSITY = "active"
REST_INTENSITIES = ("rest", "recovery")
SPORT_FAMILY = {"running": "run", "cycling": "bike", "swimming": "swim"}  # FIT sport names, not Garmin typeKeys

Status = Literal["ok", "parse_error", "no_fit"]


@dataclass
class FitResult:
    status: Status
    summary: dict[str, Any] | None = None
    fit_bytes: int = 0
    error: str | None = None
    sport: str | None = None
    sub_sport: str | None = None
    device: str | None = None
    n_sessions: int = 0
    n_laps: int = 0
    n_records: int = 0


class MemberTooLargeError(ValueError):
    pass


# -- archive ------------------------------------------------------------------------------------------------------
def is_fit(data: bytes) -> bool:
    """The FIT file header: size 12 or 14, then '.FIT' at offset 8 (what ``Decoder.is_fit`` checks)."""
    return len(data) >= 12 and data[0] in (12, 14) and data[8:12] == b".FIT"


def extract_fit(zip_bytes: bytes) -> bytes | None:
    """Bytes already FIT are returned as is; otherwise the first ZIP member named ``*.fit``. ``None`` when the bytes
    are not a ZIP or hold no such member. Raises ``MemberTooLargeError`` above 50 MB."""
    if is_fit(zip_bytes):
        return zip_bytes
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None
    with zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".fit"):
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                raise MemberTooLargeError("member too large")
            with zf.open(info) as fh:
                data = fh.read(MAX_MEMBER_BYTES + 1)  # the declared size can lie
            if len(data) > MAX_MEMBER_BYTES:
                raise MemberTooLargeError("member too large")
            return data
    return None


def parse_archive(data: bytes, *, hr_zone_floors: list[int] | None = None, max_laps: int = 300,
                  series_points: int = 48) -> FitResult:
    """A downloaded body (ZIP, or a bare FIT) -> FitResult: ``no_fit`` for a valid ZIP without a FIT member (GPX/TCX
    uploads), ``parse_error`` for anything unreadable."""
    try:
        fit = extract_fit(data)
    except MemberTooLargeError:
        return _failed(0, "member too large")
    except Exception as exc:  # noqa: BLE001 - corrupt member, encrypted ZIP, ...: never crash a sync
        return _failed(0, f"unreadable archive: {type(exc).__name__}")
    if fit is None:
        if zipfile.is_zipfile(io.BytesIO(data)):
            return FitResult(status="no_fit", error="no .fit file in the archive")
        return _failed(0, "not a ZIP or FIT file")
    return parse_fit(fit, hr_zone_floors=hr_zone_floors, max_laps=max_laps, series_points=series_points)


def _failed(fit_bytes: int, error: str) -> FitResult:
    return FitResult(status="parse_error", fit_bytes=fit_bytes, error=redact(error)[:500])


# -- decoding -----------------------------------------------------------------------------------------------------
def _profile_version(data: bytes) -> str | None:
    if len(data) < 4:
        return None
    pv = struct.unpack_from("<H", data, 2)[0]
    return f"{pv / 1000:g}" if pv > 2199 else f"{pv / 100:g}"  # the SDK's rule: x1000 from 22.0, x100 before


def _decode(fit_bytes: bytes) -> tuple[dict[str, Any], list[Exception]]:
    from garmin_fit_sdk import Decoder, Stream

    decoder = Decoder(Stream.from_byte_array(bytearray(fit_bytes)))
    messages, errors = decoder.read(
        apply_scale_and_offset=True, convert_datetimes_to_dates=True, convert_types_to_strings=True,
        enable_crc_check=False,  # the ZIP already carries a CRC32
        expand_sub_fields=True, expand_components=True, merge_heart_rates=True,
    )
    return dict(messages), list(errors)


def parse_fit(fit_bytes: bytes, *, hr_zone_floors: list[int] | None = None, max_laps: int = 300,
              series_points: int = 48) -> FitResult:
    n = len(fit_bytes)
    try:
        messages, errors = _decode(fit_bytes)
    except Exception as exc:  # noqa: BLE001 - a broken file is a parse_error, never an exception
        return _failed(n, f"decode failed: {type(exc).__name__}: {exc}")
    sessions = [s for s in messages.get("session_mesgs") or [] if isinstance(s, dict)]
    if not sessions:
        return _failed(n, "no session message" + (f" ({type(errors[0]).__name__}: {errors[0]})" if errors else ""))
    try:
        summary = _summarize(messages, errors, sessions, _profile_version(fit_bytes), hr_zone_floors, max(max_laps, 1),
                             max(series_points, 1))
    except Exception as exc:  # noqa: BLE001
        return _failed(n, f"summary failed: {type(exc).__name__}: {exc}")
    session = summary.get("session", {})
    meta = summary["meta"]
    return FitResult(status="ok", summary=summary, fit_bytes=n, sport=session.get("sport"),
                     sub_sport=session.get("sub_sport"), device=summary.get("device", {}).get("product"),
                     n_sessions=meta["n_sessions"], n_laps=meta["n_laps"], n_records=meta["n_records"])


# -- value helpers ------------------------------------------------------------------------------------------------
def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v) if math.isfinite(v) else None


def _r(v: Any, nd: int = 1) -> float | int | None:
    x = _num(v)
    if x is None:
        return None
    return round(x) if nd == 0 else round(x, nd)


def _div(v: Any, by: float, nd: int) -> float | int | None:
    x = _num(v)
    return None if x is None else _r(x / by, nd)


def _first(d: dict[str, Any], *keys: str) -> Any:
    return next((d[k] for k in keys if d.get(k) is not None), None)


def _enum(v: Any) -> str | None:
    """Profile types come back as strings; a value unknown to the SDK's profile stays a number: keep it as text."""
    if v is None or isinstance(v, bool):
        return None
    return v if isinstance(v, str) else str(v)


def _text(v: Any) -> str | None:
    return v.strip() or None if isinstance(v, str) else None


def _iso(v: Any) -> str | None:
    if not isinstance(v, datetime):
        return None
    dt = v if v.tzinfo else v.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _deg(v: Any) -> float | None:
    x = _num(v)
    return None if x is None else round(x * SEMICIRCLE_DEG, 4)


def _ints(v: Any) -> list[int] | None:
    values = v if isinstance(v, list) else [v]
    nums = [_num(x) for x in values]
    if not any(x is not None for x in nums):
        return None
    return [round(x) if x is not None else 0 for x in nums]


def _mean(values: Iterable[float]) -> float | None:
    v = list(values)
    return sum(v) / len(v) if v else None


def _spm(d: dict[str, Any]) -> int | None:
    """Running cadence in steps per minute: FIT stores strides (one foot) plus a fractional part."""
    c = _num(d.get("avg_cadence"))
    return None if c is None else round((c + (_num(d.get("avg_fractional_cadence")) or 0)) * 2)


def _balance_right(v: Any) -> float | None:
    """``left_right_balance_100``: bit 15 = the value is the right side, low 14 bits = percent x 100. The SDK does
    not expand it (checked with garmin-fit-sdk 21.218.0). Without the right-side bit the side is unknown: omitted."""
    if isinstance(v, float) and 0 <= v <= 100:
        return round(v, 1)  # already a percentage (a future SDK expanding the type)
    if isinstance(v, int) and not isinstance(v, bool) and v & 0x8000:
        return round((v & 0x3FFF) / 100, 1)
    return None


def _rpe(v: Any) -> float | None:
    """``workout_rpe`` has scale 1 in profile 21.218: devices write 10-100 for RPE 1-10 (D-078)."""
    x = _num(v)
    if x is None:
        return None
    return round(x / 10 if x > 10 else x, 1)


def _pair(d: dict[str, Any], left: str, right: str, nd: int) -> list[float | int | None] | None:
    a, b = _r(d.get(left), nd), _r(d.get(right), nd)
    return [a, b] if a is not None and b is not None else None


def _phase(v: Any) -> list[int] | None:
    values = [x for x in (v if isinstance(v, list) else []) if _num(x) is not None]
    return [round(values[0]), round(values[1])] if len(values) >= 2 else None


def _phase_pair(d: dict[str, Any], left: str, right: str) -> list[list[int]] | None:
    a, b = _phase(d.get(left)), _phase(d.get(right))
    return [a, b] if a and b else None


def prune(obj: Any) -> Any:
    """Drop ``None`` values and empty objects, recursively (lists keep their length)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v = prune(v)
            if v is None or (isinstance(v, dict) and not v):
                continue
            out[k] = v
        return out
    if isinstance(obj, list):
        return [prune(x) for x in obj]
    return obj


# -- sections -----------------------------------------------------------------------------------------------------
def _family(s: dict[str, Any]) -> str | None:
    return SPORT_FAMILY.get(str(s.get("sport")))


def _session(s: dict[str, Any]) -> dict[str, Any]:
    run = _family(s) == "run"
    start = s.get("start_time")
    elapsed, timer = _num(s.get("total_elapsed_time")), _num(s.get("total_timer_time"))
    end = start + timedelta(seconds=elapsed) if isinstance(start, datetime) and elapsed is not None else None
    return {
        "sport": _enum(s.get("sport")), "sub_sport": _enum(s.get("sub_sport")),
        "profile_name": _text(s.get("sport_profile_name")),
        "start_time": _iso(start), "end_time": _iso(end),
        "elapsed_s": _r(elapsed), "timer_s": _r(timer), "moving_s": _r(s.get("total_moving_time")),
        "paused_s": _r(max(elapsed - timer, 0)) if elapsed is not None and timer is not None else None,
        "distance_m": _r(s.get("total_distance")), "ascent_m": _r(s.get("total_ascent"), 0),
        "descent_m": _r(s.get("total_descent"), 0), "calories_kcal": _r(s.get("total_calories"), 0),
        "avg_speed_mps": _r(_first(s, "enhanced_avg_speed", "avg_speed"), 2),
        "max_speed_mps": _r(_first(s, "enhanced_max_speed", "max_speed"), 2),
        "avg_gap_mps": _r(s.get("avg_grade_adjusted_speed"), 2),
        "avg_hr": _r(s.get("avg_heart_rate"), 0), "max_hr": _r(s.get("max_heart_rate"), 0),
        "min_hr": _r(s.get("min_heart_rate"), 0),
        "avg_cadence": _r(s.get("avg_cadence"), 0), "max_cadence": _r(s.get("max_cadence"), 0),
        "avg_cadence_spm": _spm(s) if run else None,
        "avg_power": _r(s.get("avg_power"), 0), "max_power": _r(s.get("max_power"), 0),
        "normalized_power": _r(s.get("normalized_power"), 0), "ftp": _r(s.get("threshold_power"), 0),
        "tss": _r(s.get("training_stress_score")), "intensity_factor": _r(s.get("intensity_factor"), 2),
        "work_kj": _div(s.get("total_work"), 1000, 0), "training_load_peak": _r(s.get("training_load_peak")),
        "aerobic_te": _r(s.get("total_training_effect")), "anaerobic_te": _r(s.get("total_anaerobic_training_effect")),
        "avg_temperature_c": _r(s.get("avg_temperature"), 0),
        "avg_respiration_rate": _r(_first(s, "enhanced_avg_respiration_rate", "avg_respiration_rate")),
        "avg_spo2": _r(s.get("avg_spo2"), 0), "avg_stress": _r(s.get("avg_stress"), 0),
        "rmssd_hrv_ms": _r(s.get("rmssd_hrv")), "sdrr_hrv_ms": _r(s.get("sdrr_hrv")),
        "rpe": _rpe(s.get("workout_rpe")), "feel": _r(s.get("workout_feel"), 0),
        "pool_length_m": _r(s.get("pool_length")), "num_lengths": _r(s.get("num_lengths"), 0),
        "num_active_lengths": _r(s.get("num_active_lengths"), 0), "avg_swolf": _r(s.get("avg_swolf"), 0),
        "stroke": _enum(s.get("swim_stroke")), "num_laps": _r(s.get("num_laps"), 0),
    }


def _lap(lap: dict[str, Any], i: int, run: bool) -> dict[str, Any]:
    return {
        "i": i, "start_time": _iso(lap.get("start_time")), "elapsed_s": _r(lap.get("total_elapsed_time")),
        "timer_s": _r(lap.get("total_timer_time")), "distance_m": _r(lap.get("total_distance")),
        "avg_speed_mps": _r(_first(lap, "enhanced_avg_speed", "avg_speed"), 2),
        "max_speed_mps": _r(_first(lap, "enhanced_max_speed", "max_speed"), 2),
        "avg_hr": _r(lap.get("avg_heart_rate"), 0), "max_hr": _r(lap.get("max_heart_rate"), 0),
        "avg_power": _r(lap.get("avg_power"), 0), "max_power": _r(lap.get("max_power"), 0),
        "normalized_power": _r(lap.get("normalized_power"), 0), "avg_cadence": _r(lap.get("avg_cadence"), 0),
        "avg_cadence_spm": _spm(lap) if run else None,
        "ascent_m": _r(lap.get("total_ascent"), 0), "descent_m": _r(lap.get("total_descent"), 0),
        "intensity": _enum(lap.get("intensity")), "trigger": _enum(lap.get("lap_trigger")),
        "wkt_step_index": _r(lap.get("wkt_step_index"), 0),
        "balance_right_pct": _balance_right(lap.get("left_right_balance")),
        "step_length_m": _div(lap.get("avg_step_length"), 1000, 2),
        "vertical_oscillation_mm": _r(lap.get("avg_vertical_oscillation")),
        "stance_time_ms": _r(lap.get("avg_stance_time"), 0),
        "stance_time_balance_pct": _r(lap.get("avg_stance_time_balance")),
        "vertical_ratio_pct": _r(lap.get("avg_vertical_ratio")), "avg_gap_mps": _r(lap.get("avg_grade_adjusted_speed"), 2),
        "num_lengths": _r(lap.get("num_lengths"), 0), "stroke": _enum(lap.get("swim_stroke")),
        "avg_swolf": _r(lap.get("avg_swolf"), 0),
    }


def _intervals(laps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Work = 'active' laps, rest = 'rest'/'recovery' laps; only with >= 2 work laps and >= 1 rest lap."""
    work = [x for x in laps if x.get("intensity") == WORK_INTENSITY]
    rest = [x for x in laps if x.get("intensity") in REST_INTENSITIES]
    if len(work) < 2 or not rest:
        return None

    def avg(rows: list[dict[str, Any]], key: str, nd: int) -> float | int | None:
        return _r(_mean(x[key] for x in rows if x.get(key) is not None), nd)

    first, last = work[0], work[-1]
    s0, s1 = first.get("avg_speed_mps"), last.get("avg_speed_mps")
    h0, h1 = first.get("avg_hr"), last.get("avg_hr")
    max_hrs = [x["max_hr"] for x in work if x.get("max_hr") is not None]
    return {
        "work": {"n": len(work), "avg_duration_s": avg(work, "timer_s", 1), "avg_distance_m": avg(work, "distance_m", 1),
                 "avg_hr": avg(work, "avg_hr", 0), "max_hr": max(max_hrs) if max_hrs else None,
                 "avg_power": avg(work, "avg_power", 0), "avg_speed_mps": avg(work, "avg_speed_mps", 2),
                 "first_to_last_speed_pct": _r((s1 - s0) / s0 * 100) if s0 and s1 is not None else None,
                 "first_to_last_hr": h1 - h0 if h0 is not None and h1 is not None else None},
        "rest": {"n": len(rest), "avg_duration_s": avg(rest, "timer_s", 1), "avg_hr": avg(rest, "avg_hr", 0),
                 "avg_speed_mps": avg(rest, "avg_speed_mps", 2)},
        "lap_indexes": {"work": [x["i"] for x in work], "rest": [x["i"] for x in rest]},
    }


def _dynamics(s: dict[str, Any]) -> dict[str, Any] | None:
    fam = _family(s)
    if fam == "run":
        return {"run": {
            "cadence_spm": _spm(s), "step_length_m": _div(s.get("avg_step_length"), 1000, 2),
            "vertical_oscillation_mm": _r(s.get("avg_vertical_oscillation")),
            "vertical_ratio_pct": _r(s.get("avg_vertical_ratio")), "stance_time_ms": _r(s.get("avg_stance_time"), 0),
            "stance_time_balance_pct": _r(s.get("avg_stance_time_balance")),
            "gap_mps": _r(s.get("avg_grade_adjusted_speed"), 2),
        }}
    if fam == "bike":
        return {"bike": {
            "balance_right_pct": _balance_right(s.get("left_right_balance")),
            "torque_effectiveness_pct": _pair(s, "avg_left_torque_effectiveness", "avg_right_torque_effectiveness", 1),
            "pedal_smoothness_pct": _pair(s, "avg_left_pedal_smoothness", "avg_right_pedal_smoothness", 1),
            "time_standing_s": _r(s.get("time_standing"), 0), "stand_count": _r(s.get("stand_count"), 0),
            "pco_mm": _pair(s, "avg_left_pco", "avg_right_pco", 0),
            "power_phase_deg": _phase_pair(s, "avg_left_power_phase", "avg_right_power_phase"),
            "power_phase_peak_deg": _phase_pair(s, "avg_left_power_phase_peak", "avg_right_power_phase_peak"),
        }}
    if fam == "swim":
        return {"swim": {
            "swolf": _r(s.get("avg_swolf"), 0), "stroke_distance_m": _r(s.get("avg_stroke_distance"), 2),
            "strokes_per_length": _r(s.get("avg_strokes_per_length")), "stroke": _enum(s.get("swim_stroke")),
            "pool_length_m": _r(s.get("pool_length")),
        }}
    return None


def _zones(messages: dict[str, Any], s: dict[str, Any], computed: list[int] | None,
           floors: list[int] | None) -> dict[str, Any] | None:
    tiz = next((m for m in messages.get("time_in_zone_mesgs") or []
                if isinstance(m, dict) and m.get("reference_mesg") == "session"
                and m.get("reference_index") in (0, None)), None)
    if tiz is not None:
        hr = {"time_s": _ints(tiz.get("time_in_hr_zone")), "boundaries_bpm": _ints(tiz.get("hr_zone_high_boundary")),
              "calc_type": _enum(tiz.get("hr_calc_type")), "max_hr": _r(tiz.get("max_heart_rate"), 0),
              "resting_hr": _r(tiz.get("resting_heart_rate"), 0),
              "threshold_hr": _r(tiz.get("threshold_heart_rate"), 0)}
        power = {"time_s": _ints(tiz.get("time_in_power_zone")),
                 "boundaries_w": _ints(tiz.get("power_zone_high_boundary")),
                 "ftp": _r(tiz.get("functional_threshold_power"), 0), "calc_type": _enum(tiz.get("pwr_calc_type"))}
        if hr["time_s"] or power["time_s"]:
            return {"source": "time_in_zone", "hr": hr if hr["time_s"] else None,
                    "power": power if power["time_s"] else None}
    hr_s, pw_s = _ints(s.get("time_in_hr_zone")), _ints(s.get("time_in_power_zone"))
    if hr_s or pw_s:
        return {"source": "session", "hr": {"time_s": hr_s} if hr_s else None,
                "power": {"time_s": pw_s} if pw_s else None}
    if computed:
        return {"source": "computed", "hr": {"time_s": computed, "floors_bpm": floors}}
    return None


def _device(messages: dict[str, Any]) -> dict[str, Any] | None:
    infos = [d for d in messages.get("device_info_mesgs") or [] if isinstance(d, dict)]
    file_id: dict[str, Any] = next(iter(messages.get("file_id_mesgs") or []), {}) or {}
    creator = next((d for d in infos if d.get("device_index") == "creator"), infos[0] if infos else None)

    def product(d: dict[str, Any]) -> str | None:
        p = _first(d, "garmin_product", "product_name", "product")
        return None if p is None else str(p)

    src = creator or file_id
    sensors = sorted({v for d in infos if d is not creator and d.get("device_index") != "creator"
                      for v in (d.get("antplus_device_type"), d.get("ble_device_type")) if isinstance(v, str)})
    return {
        "manufacturer": _enum(src.get("manufacturer") or file_id.get("manufacturer")),
        "product": product(src) or product(file_id),
        "software_version": _r((creator or {}).get("software_version"), 2),
        "sensors": sensors or None,  # serial numbers are never copied (D-075)
    }


def _dev_power_key(messages: dict[str, Any]) -> tuple[int, str] | None:
    """The decoder keys developer values by the index of their field_description message."""
    for key, d in enumerate(messages.get("field_description_mesgs") or []):
        name = d.get("field_name") if isinstance(d, dict) else None
        if isinstance(name, str) and name.strip().lower() in DEV_POWER_NAMES:
            return key, name.strip()
    return None


class _Records:
    """One pass over the record messages: aggregates, decoupling, the coarse series, computed zones, route ends.
    The records themselves are not kept."""

    def __init__(self, messages: dict[str, Any], s: dict[str, Any], start: datetime | None) -> None:
        raw = [r for r in messages.get("record_mesgs") or [] if isinstance(r, dict)]
        self.n = len(raw)
        self.recs = sorted((r for r in raw if isinstance(r.get("timestamp"), datetime)), key=lambda r: r["timestamp"])
        self.family = _family(s)
        self.start = start or (self.recs[0]["timestamp"] if self.recs else None)
        deltas = [(b["timestamp"] - a["timestamp"]).total_seconds() for a, b in zip(self.recs, self.recs[1:], strict=False)]
        deltas = [d for d in deltas if d > 0]
        self.interval = max(round(statistics.median(deltas)), 1) if deltas else 1
        dev = _dev_power_key(messages)
        self.power: list[float | None] = []
        for r in self.recs:
            p = _num(r.get("power"))
            if p is None and dev is not None:
                v = (r.get("developer_fields") or {}).get(dev[0])
                p = _num(v[0] if isinstance(v, list) and v else v)
            self.power.append(p)
        has_record_power = any(_num(r.get("power")) is not None for r in self.recs)
        self.power_source = "record.power" if has_record_power else (
            f"developer:{dev[1]}" if dev and any(p is not None for p in self.power) else None)
        self._timeline(messages.get("event_mesgs") or [])

    def _timeline(self, events: list[Any]) -> None:
        """Timer seconds at each record; records inside a timer stop are paused. Without timer events, gaps larger
        than twice the recording interval are treated as pauses."""
        windows: list[tuple[datetime, datetime]] = []
        stopped_at: datetime | None = None
        timer_events = sorted((e for e in events if isinstance(e, dict) and e.get("event") == "timer"
                               and isinstance(e.get("timestamp"), datetime)), key=lambda e: e["timestamp"])
        for e in timer_events:
            if e.get("event_type") in TIMER_STOPS and stopped_at is None:
                stopped_at = e["timestamp"]
            elif e.get("event_type") == "start" and stopped_at is not None:
                windows.append((stopped_at, e["timestamp"]))
                stopped_at = None
        if stopped_at is not None:
            windows.append((stopped_at, datetime.max.replace(tzinfo=UTC)))
        self.active: list[bool] = []
        self.timer_t: list[float] = []
        t = 0.0
        prev: datetime | None = None
        for r in self.recs:
            ts = r["timestamp"]
            self.active.append(not any(a < ts < b for a, b in windows))
            if prev is not None:
                delta = (ts - prev).total_seconds()
                if timer_events:
                    overlap = sum(max((min(ts, b) - max(prev, a)).total_seconds(), 0) for a, b in windows)
                    t += max(delta - overlap, 0)
                elif delta <= 2 * self.interval:
                    t += delta
            self.timer_t.append(t)
            prev = ts

    def _coverage(self, values: list[Any]) -> float | None:
        return round(sum(v is not None for v in values) / self.n * 100, 1) if self.n else None

    def block(self, timer_s: float | None) -> dict[str, Any]:
        hr = [_num(r.get("heart_rate")) for r in self.recs]
        speed = [_num(_first(r, "enhanced_speed", "speed")) for r in self.recs]
        cadence = [_num(r.get("cadence")) for r in self.recs]
        pos = [r.get("position_lat") if r.get("position_long") is not None else None for r in self.recs]
        cov = {"hr": self._coverage(hr), "power": self._coverage(self.power), "cadence": self._coverage(cadence),
               "position": self._coverage(pos)}
        out: dict[str, Any] = {"n": self.n, "interval_s": self.interval if self.recs else None, "coverage_pct": cov,
                               "decoupling_pct": None, "power_zero_pct": None, "power_source": self.power_source}
        output: list[float | None] | None = None
        if self.family == "bike" and self.power_source:
            output = self.power
        elif self.family == "run":
            output = speed
        total = timer_s if timer_s is not None else (self.timer_t[-1] if self.timer_t else 0)
        if (output is not None and total >= MIN_DECOUPLING_TIMER_S and (cov["hr"] or 0) >= MIN_DECOUPLING_COVERAGE
                and (self._coverage(output) or 0) >= MIN_DECOUPLING_COVERAGE):
            out["decoupling_pct"] = self._decoupling(hr, output)
        if self.family == "bike":
            watts = [p for p in self.power if p is not None]
            if watts:
                out["power_zero_pct"] = round(sum(p == 0 for p in watts) / len(watts) * 100, 1)
        return out

    def _decoupling(self, hr: list[float | None], output: list[float | None]) -> float | None:
        """(first-half output:HR - second-half output:HR) / first half x 100, halves split by timer time."""
        half = (self.timer_t[-1] if self.timer_t else 0) / 2
        halves: tuple[list[tuple[float, float]], list[tuple[float, float]]] = ([], [])
        for t, ok, h, o in zip(self.timer_t, self.active, hr, output, strict=True):
            if ok and h and o is not None:
                halves[0 if t <= half else 1].append((o, h))
        ratios = []
        for pts in halves:
            if not pts:
                return None
            ratios.append(sum(o for o, _ in pts) / sum(h for _, h in pts))
        return round((ratios[0] - ratios[1]) / ratios[0] * 100, 1) if ratios[0] else None

    def series(self, timer_s: float | None, points: int) -> list[dict[str, Any]]:
        if not self.recs or self.start is None:
            return []
        timer = timer_s or (self.timer_t[-1] if self.timer_t else 0) or 1
        bucket_s = max(60, math.ceil(timer / points))
        rows = self._buckets(bucket_s)
        if len(rows) > points:  # long pauses with records: widen the buckets over the whole elapsed span
            span = max(int((r["timestamp"] - self.start).total_seconds()) for r in self.recs) + 1
            rows = self._buckets(max(bucket_s, math.ceil(span / points)))
        return rows

    def _buckets(self, bucket_s: int) -> list[dict[str, Any]]:
        assert self.start is not None
        acc: dict[int, dict[str, list[float]]] = {}
        last: dict[int, dict[str, float]] = {}
        for r, ok, p in zip(self.recs, self.active, self.power, strict=True):
            if not ok:
                continue
            b = max(int((r["timestamp"] - self.start).total_seconds()), 0) // bucket_s
            a = acc.setdefault(b, {"hr": [], "power": [], "speed_mps": [], "cadence": []})
            for key, v in (("hr", r.get("heart_rate")), ("power", p),
                           ("speed_mps", _first(r, "enhanced_speed", "speed")), ("cadence", r.get("cadence"))):
                x = _num(v)
                if x is not None:
                    a[key].append(x)
            lb = last.setdefault(b, {})
            for key, v in (("alt_m", _first(r, "enhanced_altitude", "altitude")), ("dist_m", r.get("distance"))):
                x = _num(v)
                if x is not None:
                    lb[key] = x
        out = []
        for b in sorted(acc):
            a, lb = acc[b], last.get(b, {})
            out.append({"t_s": b * bucket_s, "hr": _r(_mean(a["hr"]), 0), "power": _r(_mean(a["power"]), 0),
                        "speed_mps": _r(_mean(a["speed_mps"]), 2), "cadence": _r(_mean(a["cadence"]), 0),
                        "alt_m": _r(lb.get("alt_m"), 0), "dist_m": _r(lb.get("dist_m"), 0)})
        return out

    def computed_zones(self, floors: list[int] | None) -> list[int] | None:
        """HR time in zones from the records and the athlete's Garmin zone floors: index 0 = below zone 1."""
        if not floors or len(floors) != 5 or sorted(floors) != floors:
            return None
        time_s = [0] * 6
        seen = False
        for r, ok in zip(self.recs, self.active, strict=True):
            h = _num(r.get("heart_rate"))
            if ok and h is not None:
                time_s[bisect.bisect_right(floors, h)] += self.interval
                seen = True
        return time_s if seen else None

    def ends(self) -> tuple[list[float | None] | None, list[float | None] | None]:
        with_pos = [r for r in self.recs if _num(r.get("position_lat")) is not None
                    and _num(r.get("position_long")) is not None]
        if not with_pos:
            return None, None
        a, b = with_pos[0], with_pos[-1]
        return ([_deg(a["position_lat"]), _deg(a["position_long"])],
                [_deg(b["position_lat"]), _deg(b["position_long"])])


def _route(s: dict[str, Any], recs: _Records) -> dict[str, Any] | None:
    start: list[float | None] | None = None
    end: list[float | None] | None = None
    if _num(s.get("start_position_lat")) is not None and _num(s.get("start_position_long")) is not None:
        start = [_deg(s["start_position_lat"]), _deg(s["start_position_long"])]
    if _num(s.get("end_position_lat")) is not None and _num(s.get("end_position_long")) is not None:
        end = [_deg(s["end_position_lat"]), _deg(s["end_position_long"])]
    if start is None or end is None:
        first, last = recs.ends()
        start, end = start or first, end or last
    return {"start": start, "end": end} if start or end else None


def _summarize(messages: dict[str, Any], errors: list[Exception], sessions: list[dict[str, Any]],
               profile_version: str | None, floors: list[int] | None, max_laps: int,
               series_points: int) -> dict[str, Any]:
    from garmin_fit_sdk import __version__ as sdk_version

    s = sessions[0]
    run = _family(s) == "run"
    file_id: dict[str, Any] = next(iter(messages.get("file_id_mesgs") or []), {}) or {}
    raw_laps = [x for x in messages.get("lap_mesgs") or [] if isinstance(x, dict)]
    raw_laps = [x for x in raw_laps if x.get("total_timer_time") is not None or x.get("total_distance") is not None]
    laps = [_lap(x, i, run) for i, x in enumerate(raw_laps, 1)]
    start = s.get("start_time") if isinstance(s.get("start_time"), datetime) else None
    timer_s = _num(s.get("total_timer_time"))
    recs = _Records(messages, s, start)
    summary: dict[str, Any] = {
        "meta": {
            "parser_version": PARSER_VERSION, "sdk_version": sdk_version, "profile_version": profile_version,
            "file_type": _enum(file_id.get("type")), "time_created": _iso(file_id.get("time_created")),
            "n_sessions": len(sessions), "n_laps": len(raw_laps), "n_records": recs.n,
            "laps_truncated": len(laps) > max_laps, "truncated": False,
            "errors": [redact(f"{type(e).__name__}: {e}")[:200] for e in errors[:MAX_ERRORS]],
        },
        "session": _session(s),
        "sessions": [_session(x) for x in sessions] if len(sessions) > 1 else None,
        "zones": _zones(messages, s, recs.computed_zones(floors), floors),
        "laps": laps[:max_laps],
        "intervals": _intervals(laps),
        "dynamics": _dynamics(s),
        "records": recs.block(timer_s),
        "series": recs.series(timer_s, series_points),
        "route": _route(s, recs),
        "device": _device(messages),
    }
    return _cap(prune(summary))


def _cap(summary: dict[str, Any]) -> dict[str, Any]:
    """Hard size cap (D-075): drop the series, then keep 100 laps, then drop the laps."""
    def size() -> int:
        return len(json.dumps(summary, separators=(",", ":"), ensure_ascii=False))

    if size() <= MAX_JSON_CHARS:
        return summary
    summary["meta"]["truncated"] = True
    summary.pop("series", None)
    if size() > MAX_JSON_CHARS and len(summary.get("laps", [])) > CUT_LAPS:
        summary["laps"] = summary["laps"][:CUT_LAPS]
        summary["meta"]["laps_truncated"] = True
    if size() > MAX_JSON_CHARS:
        summary.pop("laps", None)
    return summary
