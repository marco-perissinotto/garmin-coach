"""Deterministic synthetic FIT activities built with the official SDK's Encoder (never a real recording: real files
carry GPS tracks and device serial numbers).

The default file is an interval ride: 11 laps of ``lap_s`` seconds alternating active/rest (6 work, 5 rest), 1 Hz
records with heart rate, power, speed, distance, position, altitude and cadence, a session-level time_in_zone
message, a creator device and a heart-rate strap. Every number is derived from the parameters, so tests can
compute the expected summary independently.
"""

from __future__ import annotations

import functools
import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from garmin_fit_sdk import Encoder, Profile

MN = Profile["mesg_num"]
T0 = datetime(2026, 10, 27, 6, 0, tzinfo=UTC)
LAT0, LON0 = 45.43791, 12.33512  # a synthetic point (Venice lagoon), not anyone's home
SERIAL = 3_999_999_999  # must never appear in a summary
TIZ_HR = [0.0, 610.0, 300.0, 520.0, 440.0, 0.0]
TIZ_HR_BOUNDS = [113, 131, 148, 165, 182]
TIZ_POWER = [100.0, 200.0, 300.0, 400.0, 500.0, 300.0, 70.0]
TIZ_POWER_BOUNDS = [138, 188, 225, 263, 300, 375]


def semicircles(deg: float) -> int:
    return round(deg * 2**31 / 180)


@dataclass
class Spec:
    sport: str = "cycling"
    sub_sport: str = "road"
    laps: int = 11
    lap_s: int = 170
    hr: Callable[[int, bool, int], int] = lambda t, work, rep: 150 + rep if work else 120  # noqa: E731
    power: Callable[[int, bool, int], int | None] = lambda t, work, rep: 300 if work else 100  # noqa: E731
    speed: Callable[[int, bool, int], float] = lambda t, work, rep: 10.0 if work else 6.0  # noqa: E731
    cadence: Callable[[int, bool], int] = lambda t, work: 90 if work else 80  # noqa: E731
    time_in_zone: bool = True
    session_zones: bool = False
    positions: bool = True
    dev_power: bool = False  # power only as a developer field named "Power"
    pause: tuple[int, int] | None = None  # (stop_s, start_s): timer stopped, no records in between
    extra: dict[str, Any] = field(default_factory=dict)  # extra session fields


def _rep_of(lap_index: int) -> int:
    return lap_index // 2  # laps 0,2,4,... are work reps 0,1,2,...


def build_fit(spec: Spec | None = None) -> bytes:
    s = spec or Spec()
    enc = Encoder()
    end_s = s.laps * s.lap_s + (s.pause[1] - s.pause[0] if s.pause else 0)
    t_end = T0 + timedelta(seconds=end_s)
    enc.write_mesg({"mesg_num": MN["FILE_ID"], "type": "activity", "manufacturer": "garmin", "product": 3906,
                    "time_created": T0, "serial_number": SERIAL})
    enc.write_mesg({"mesg_num": MN["DEVICE_INFO"], "timestamp": T0, "device_index": "creator",
                    "manufacturer": "garmin", "product": 3906, "software_version": 16.22, "serial_number": SERIAL})
    enc.write_mesg({"mesg_num": MN["DEVICE_INFO"], "timestamp": T0, "device_index": 1, "source_type": "antplus",
                    "device_type": 120, "manufacturer": "garmin", "serial_number": SERIAL + 1})  # 120 = heart_rate
    dev_key = 0
    if s.dev_power:
        dev_id = {"mesg_num": MN["DEVELOPER_DATA_ID"], "developer_data_index": 0, "application_version": 1}
        desc = {"mesg_num": MN["FIELD_DESCRIPTION"], "developer_data_index": 0, "field_definition_number": 0,
                "fit_base_type_id": 0x84,  # uint16
                "field_name": "Power", "units": "Watts", "native_mesg_num": 20}
        enc.write_mesg(dev_id)
        enc.write_mesg(desc)
        enc.add_developer_field(dev_key, dev_id, desc)
    enc.write_mesg({"mesg_num": MN["EVENT"], "timestamp": T0, "event": "timer", "event_type": "start"})

    dist = 0.0
    lap_rows: list[dict[str, Any]] = []
    for lap in range(s.laps):
        work = lap % 2 == 0
        rep = _rep_of(lap)
        rows = []
        for k in range(s.lap_s):
            t = lap * s.lap_s + k
            wall = t
            if s.pause and t >= s.pause[0]:
                if t == s.pause[0]:
                    enc.write_mesg({"mesg_num": MN["EVENT"], "timestamp": T0 + timedelta(seconds=t), "event": "timer",
                                    "event_type": "stop_all"})
                    enc.write_mesg({"mesg_num": MN["EVENT"], "timestamp": T0 + timedelta(seconds=s.pause[1]),
                                    "event": "timer", "event_type": "start"})
                wall = t + s.pause[1] - s.pause[0]
            speed = s.speed(t, work, rep)
            dist += speed
            rec: dict[str, Any] = {"mesg_num": MN["RECORD"], "timestamp": T0 + timedelta(seconds=wall),
                                   "heart_rate": s.hr(t, work, rep), "enhanced_speed": speed, "distance": dist,
                                   "enhanced_altitude": 100.0 + t / 100, "cadence": s.cadence(t, work)}
            p = s.power(t, work, rep)
            if p is not None:
                if s.dev_power:
                    rec["developer_fields"] = {dev_key: p}
                else:
                    rec["power"] = p
            if s.positions:
                rec["position_lat"] = semicircles(LAT0 + t * 1e-5)
                rec["position_long"] = semicircles(LON0 + t * 1e-5)
            enc.write_mesg(rec)
            rows.append(rec)
        hrs = [r["heart_rate"] for r in rows]
        watts = [s.power(lap * s.lap_s + k, work, rep) for k in range(s.lap_s)]
        lap_msg: dict[str, Any] = {
            "mesg_num": MN["LAP"], "message_index": lap, "timestamp": rows[-1]["timestamp"],
            "start_time": rows[0]["timestamp"], "total_elapsed_time": float(s.lap_s),
            "total_timer_time": float(s.lap_s), "total_distance": s.speed(lap * s.lap_s, work, rep) * s.lap_s,
            "enhanced_avg_speed": s.speed(lap * s.lap_s, work, rep), "avg_heart_rate": round(sum(hrs) / len(hrs)),
            "max_heart_rate": max(hrs), "avg_cadence": s.cadence(lap * s.lap_s, work),
            "intensity": "active" if work else "rest", "lap_trigger": "manual", "wkt_step_index": lap,
            "left_right_balance": 0x8000 | 5080,
        }
        if None not in watts:
            lap_msg.update(avg_power=round(sum(watts) / len(watts)), max_power=max(watts))  # type: ignore[arg-type]
        lap_rows.append(lap_msg)
        enc.write_mesg(lap_msg)
    enc.write_mesg({"mesg_num": MN["EVENT"], "timestamp": t_end, "event": "timer", "event_type": "stop_all"})
    if s.time_in_zone:
        enc.write_mesg({"mesg_num": MN["TIME_IN_ZONE"], "timestamp": t_end, "reference_mesg": "session",
                        "reference_index": 0, "time_in_hr_zone": TIZ_HR, "hr_zone_high_boundary": TIZ_HR_BOUNDS,
                        "hr_calc_type": "percent_max_hr", "max_heart_rate": 185, "resting_heart_rate": 48,
                        "threshold_heart_rate": 166, "time_in_power_zone": TIZ_POWER,
                        "power_zone_high_boundary": TIZ_POWER_BOUNDS, "functional_threshold_power": 250,
                        "pwr_calc_type": "percent_ftp"})
    timer = float(s.laps * s.lap_s)
    session: dict[str, Any] = {
        "mesg_num": MN["SESSION"], "message_index": 0, "timestamp": t_end, "start_time": T0, "sport": s.sport,
        "sub_sport": s.sub_sport, "sport_profile_name": "Road", "total_elapsed_time": float(end_s),
        "total_timer_time": timer, "total_distance": dist, "total_ascent": 19, "total_descent": 2,
        "total_calories": 650, "enhanced_avg_speed": dist / timer, "enhanced_max_speed": 10.0,
        "avg_heart_rate": 140, "max_heart_rate": 155, "min_heart_rate": 118, "avg_cadence": 85, "max_cadence": 90,
        "avg_power": 209, "max_power": 300, "normalized_power": 235, "threshold_power": 250,
        "training_stress_score": 52.3, "intensity_factor": 0.94, "total_work": 390_830,
        "total_training_effect": 3.4, "total_anaerobic_training_effect": 1.8, "workout_rpe": 70,
        "left_right_balance": 0x8000 | 5120, "avg_left_torque_effectiveness": 78.0,
        "avg_right_torque_effectiveness": 79.5, "avg_left_pedal_smoothness": 21.0,
        "avg_right_pedal_smoothness": 22.5, "avg_left_pco": 2, "avg_right_pco": -1, "num_laps": s.laps,
        "first_lap_index": 0,
    }
    if s.positions:
        session.update(start_position_lat=semicircles(LAT0), start_position_long=semicircles(LON0))
    if s.session_zones:
        session.update(time_in_hr_zone=[10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
    session.update(s.extra)
    enc.write_mesg(session)
    enc.write_mesg({"mesg_num": MN["ACTIVITY"], "timestamp": t_end, "total_timer_time": timer, "num_sessions": 1,
                    "type": "manual", "event": "activity", "event_type": "stop"})
    return enc.close()


def zip_bytes(member: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, data)
    return buf.getvalue()


@functools.cache
def default_fit() -> bytes:
    return build_fit()


@functools.cache
def small_fit() -> bytes:
    """A short interval file for the sync tests (keeps every ingest fast)."""
    return build_fit(Spec(lap_s=10))


def activity_zip(activity_id: int, data: bytes | None = None) -> bytes:
    return zip_bytes(f"{activity_id}_ACTIVITY.fit", data if data is not None else small_fit())


GPX = b'<?xml version="1.0"?><gpx version="1.1" creator="test"><trk><name>t</name></trk></gpx>'
