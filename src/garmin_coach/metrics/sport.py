"""Garmin activity type keys -> sport families used by the metrics and packs."""

from __future__ import annotations

RUN = "corsa"
BIKE = "bici"
SWIM = "nuoto"
MULTI = "multisport"
STRENGTH = "forza"
OTHER = "altro"
SPORTS = (RUN, BIKE, SWIM, MULTI, STRENGTH, OTHER)

_RUN = {"running", "treadmill_running", "virtual_run", "trail_running", "track_running", "indoor_running",
        "street_running", "ultra_run"}
_BIKE = {"road_biking", "indoor_cycling", "cycling", "virtual_ride", "mountain_biking", "gravel_cycling",
         "e_bike_fitness", "e_bike_mountain", "cyclocross", "track_cycling", "bmx"}
_SWIM = {"lap_swimming", "open_water_swimming", "swimming"}
_MULTI = {"multi_sport", "triathlon", "duathlon", "transition"}
_STRENGTH = {"strength_training", "hiit", "indoor_cardio"}

# Indoor variants: their speed/power are not comparable with outdoor ones (coach note 2026-10-28).
INDOOR = {"indoor_cycling", "virtual_ride", "treadmill_running", "virtual_run", "indoor_running"}


def family(type_key: str | None) -> str:
    t = (type_key or "").lower()
    if t in _RUN:
        return RUN
    if t in _BIKE:
        return BIKE
    if t in _SWIM:
        return SWIM
    if t in _MULTI:
        return MULTI
    if t in _STRENGTH:
        return STRENGTH
    return OTHER


def is_indoor(type_key: str | None) -> bool:
    return (type_key or "").lower() in INDOOR
