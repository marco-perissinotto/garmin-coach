"""The 26 documented Personal API endpoints with window caps and refresh tiers (PLAN §3).

Only these paths exist on the Personal API host; anything else returns 404.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from garmin_personal_api import GarminPersonalApiClient


class Tier(StrEnum):
    A = "A"  # activities, TTL 60 min, synchronous
    B = "B"  # today/yesterday wellness, TTL 60 min, synchronous
    C = "C"  # last 7 days range refresh, TTL 24 h, background
    D = "D"  # single-day opt-in, TTL 24 h, background
    E = "E"  # profile-ish, TTL 7 d, background
    H = "H"  # history backfill, one-off
    F = "F"  # FIT downloads (D-071): pseudo-tier, no TTL, never requested by the gate


SYNC_TIERS = frozenset({Tier.A, Tier.B})
BACKGROUND_TIERS = frozenset({Tier.C, Tier.D, Tier.E})
REFRESH_TIERS = (Tier.A, Tier.B, Tier.C, Tier.D, Tier.E)

Kind = Literal["search", "activity", "day", "range", "static"]
Invoke = Callable[["GarminPersonalApiClient", dict[str, Any]], Any]

ACTIVITY_SEARCH_PATH = "/activitylist-service/activities/search/activities"


@dataclass(frozen=True)
class Endpoint:
    num: int
    name: str
    path: str
    kind: Kind
    domain: str  # normalization domain: activities | daily | sleep | hrv | weight | profile | raw | fit
    invoke: Invoke = field(repr=False, compare=False)
    cap_days: int | None = None
    optional_flag: str | None = None  # key in settings.optional_endpoints


def _search(c: GarminPersonalApiClient, a: dict[str, Any]) -> Any:
    # The wrapper search_activities() has no sortBy/sortOrder, so use get() with the documented params.
    params: dict[str, str | int] = {
        "start": a.get("start", 0),
        "limit": a.get("limit", 20),
        "sortBy": "startLocal",
        "sortOrder": "desc",
    }
    if a.get("start_date"):
        params["startDate"] = a["start_date"]
    if a.get("end_date"):
        params["endDate"] = a["end_date"]
    return c.get(ACTIVITY_SEARCH_PATH, params=params)


_ALL: list[Endpoint] = [
    Endpoint(1, "activities.search", ACTIVITY_SEARCH_PATH, "search", "activities", _search),
    Endpoint(2, "activity.detail", "/activity-service/activity/{activityId}", "activity", "raw",
             lambda c, a: c.get_activity(a["activity_id"])),
    Endpoint(3, "activity.workouts", "/activity-service/activity/{activityId}/workouts", "activity", "raw",
             lambda c, a: c.get_activity_workouts(a["activity_id"])),
    # The only endpoint whose body is bytes (a ZIP), not JSON: parsed by garmin_coach.fit, never stored in raw_payload.
    Endpoint(4, "activity.download", "/download-service/files/activity/{activityId}", "activity", "fit",
             lambda c, a: c.download_activity(a["activity_id"])),
    Endpoint(5, "usersummary.daily", "/usersummary-service/usersummary/daily", "day", "daily",
             lambda c, a: c.get("/usersummary-service/usersummary/daily", params={"calendarDate": a["date"]}),
             cap_days=1),
    Endpoint(6, "usersummary.range", "/usersummary-service/v2/dailysummaries", "range", "daily",
             lambda c, a: c.get_daily_summaries(a["start"], a["end"]), cap_days=365),
    Endpoint(7, "sleep.daily", "/sleep-service/sleep/dailySleep", "day", "sleep",
             lambda c, a: c.get_daily_sleep(a["date"]), cap_days=1),
    Endpoint(8, "sleep.range", "/sleep-service/sleep/dailySleepSummariesByDate", "range", "sleep",
             lambda c, a: c.get_daily_sleep_summaries(a["start"], a["end"]), cap_days=31),
    Endpoint(9, "hrv.daily", "/hrv-service/hrv/{calendarDate}", "day", "hrv",
             lambda c, a: c.get_daily_hrv(a["date"]), cap_days=1),
    Endpoint(10, "hrv.range", "/hrv-service/hrv/daily/{fromDate}/{toDate}", "range", "hrv",
             lambda c, a: c.get_daily_hrv_summaries(a["start"], a["end"]), cap_days=28),
    Endpoint(11, "hr.daily", "/wellness-service/wellness/dailyHeartRate", "day", "daily",
             lambda c, a: c.get_daily_heart_rate(a["date"]), cap_days=1),
    Endpoint(12, "bodybattery.range", "/wellness-service/wellness/bodyBattery/reports/daily", "range", "daily",
             lambda c, a: c.get_daily_body_battery(a["start"], a["end"]), cap_days=31),
    Endpoint(13, "stress.range", "/usersummary-service/stats/stress/daily/{s}/{e}", "range", "daily",
             lambda c, a: c.get_daily_stress(a["start"], a["end"]), cap_days=28),
    Endpoint(14, "steps.range", "/usersummary-service/stats/steps/daily/{s}/{e}", "range", "daily",
             lambda c, a: c.get_daily_steps(a["start"], a["end"]), cap_days=28),
    Endpoint(15, "hydration.range", "/usersummary-service/stats/hydration/daily/{s}/{e}", "range", "daily",
             lambda c, a: c.get_daily_hydration(a["start"], a["end"]), cap_days=31, optional_flag="hydration"),
    Endpoint(16, "spo2.daily", "/wellness-service/wellness/daily/spo2acclimation/{calendarDate}", "day", "raw",
             lambda c, a: c.get_daily_spo2(a["date"]), cap_days=1, optional_flag="spo2"),
    Endpoint(17, "wellnessactivity.range", "/wellnessactivity-service/activity/summary/range/{from}/{until}",
             "range", "raw", lambda c, a: c.get_wellness_activity_summaries(a["start"], a["end"]), cap_days=31),
    Endpoint(18, "weight.range", "/weight-service/weight/range/{s}/{e}", "range", "weight",
             lambda c, a: c.get_weight_measurements(a["start"], a["end"]), cap_days=31),
    Endpoint(19, "weight.all", "/weight-service/weight/all/{s}/{e}", "range", "weight",
             lambda c, a: c.get(f"/weight-service/weight/all/{a['start']}/{a['end']}"), cap_days=31),
    Endpoint(20, "weightgoal.range", "/goal-service/goal/user/effective/weightgoal/{s}/{e}", "range", "profile",
             lambda c, a: c.get_effective_weight_goals(a["start"], a["end"])),
    Endpoint(21, "hrzones", "/biometric-service/heartRateZones/v2", "static", "profile",
             lambda c, a: c.get_heart_rate_zones()),
    Endpoint(22, "fitnessage.daily", "/fitnessage-service/fitnessage/v2/{calendarDate}", "day", "profile",
             lambda c, a: c.get_fitness_age(a["date"])),
    Endpoint(23, "profile.settings", "/userprofile-service/userprofile/settings", "static", "profile",
             lambda c, a: c.get_user_profile_settings()),
    Endpoint(24, "nutrition.daily", "/nutrition-service/food/logs/{date}", "day", "raw",
             lambda c, a: c.get_daily_food_logs(a["date"]), cap_days=1, optional_flag="nutrition"),
    Endpoint(25, "menstrual.summary", "/periodichealth-service/menstrualcycle/summary/{calendarDate}", "day", "raw",
             lambda c, a: c.get_menstrual_cycle_summary(a["date"]), optional_flag="womens_health"),
    Endpoint(26, "menstrual.dayview", "/periodichealth-service/menstrualcycle/dayview/{calendarDate}", "day", "raw",
             lambda c, a: c.get_menstrual_cycle_day_view(a["date"], a["date"]), optional_flag="womens_health"),
]

ENDPOINTS: dict[str, Endpoint] = {e.name: e for e in _ALL}
assert len(ENDPOINTS) == 26, "PLAN §0.5: exactly the 26 documented endpoints"


def get(name: str) -> Endpoint:
    return ENDPOINTS[name]


# Which endpoints each refresh tier touches (PLAN §3 "Refresh tier" column).
TIER_ENDPOINTS: dict[Tier, tuple[str, ...]] = {
    Tier.A: ("activities.search", "activity.detail", "activity.workouts"),
    Tier.B: ("usersummary.daily", "sleep.daily", "hrv.daily", "hr.daily", "bodybattery.range"),
    Tier.C: (
        "usersummary.range", "sleep.range", "hrv.range", "bodybattery.range", "stress.range", "steps.range",
        "hydration.range", "wellnessactivity.range", "weight.range", "weight.all",
    ),
    Tier.D: ("spo2.daily", "nutrition.daily", "menstrual.summary", "menstrual.dayview"),
    Tier.E: ("weightgoal.range", "hrzones", "fitnessage.daily", "profile.settings"),
    # Backfill: C range endpoints over 365 days (weight.range skipped: weight.all has every weigh-in) + E + activity list.
    Tier.H: (
        "activities.search", "activity.detail", "activity.workouts", "usersummary.range", "sleep.range",
        "hrv.range", "bodybattery.range", "stress.range", "steps.range", "hydration.range",
        "wellnessactivity.range", "weight.all", "weightgoal.range", "hrzones", "fitnessage.daily",
        "profile.settings",
    ),
    Tier.F: ("activity.download",),
}
