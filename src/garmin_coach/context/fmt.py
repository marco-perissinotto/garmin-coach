"""Formatting helpers for the Markdown packs (Italian labels, the owner's advice_language)."""

from __future__ import annotations

from datetime import date

WEEKDAYS_IT = ("lun", "mar", "mer", "gio", "ven", "sab", "dom")

TYPE_LABELS = {
    "running": "corsa", "treadmill_running": "tapis roulant", "virtual_run": "corsa virtuale",
    "trail_running": "trail", "track_running": "pista", "road_biking": "bici strada", "indoor_cycling": "rulli",
    "cycling": "bici", "virtual_ride": "bici virtuale", "mountain_biking": "mtb", "gravel_cycling": "gravel",
    "lap_swimming": "piscina", "open_water_swimming": "acque libere", "multi_sport": "multisport",
    "strength_training": "forza", "walking": "camminata", "hiking": "escursione", "breathwork": "respirazione",
}


SLEEP_QUALITY_IT = {"EXCELLENT": "eccellente", "GOOD": "buono", "FAIR": "discreto", "POOR": "scarso"}
TE_LABEL_IT = {"RECOVERY": "recupero", "AEROBIC_BASE": "base aerobica", "TEMPO": "tempo",
               "LACTATE_THRESHOLD": "soglia", "VO2MAX": "VO2max", "ANAEROBIC_CAPACITY": "capacità anaerobica",
               "SPEED": "velocità", "NO_BENEFIT": "nessun beneficio"}


def sleep_quality(q: str | None) -> str | None:
    return None if not q else SLEEP_QUALITY_IT.get(q, q.lower())


def te_label(label: str | None) -> str | None:
    return None if not label else TE_LABEL_IT.get(label, label.lower().replace("_", " "))


def type_label(type_key: str | None) -> str:
    return TYPE_LABELS.get(type_key or "", (type_key or "attività").replace("_", " "))


def day_label(d: date) -> str:
    return f"{WEEKDAYS_IT[d.weekday()]} {d.day:02d}/{d.month:02d}"


def duration(seconds: float | None) -> str:
    if not seconds:
        return "–"
    m = round(seconds / 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}'" if h else f"{m}'"


def hours(seconds: float | None) -> str:
    return "–" if not seconds else f"{seconds / 3600:.1f} h"


def km(meters: float | None, decimals: int = 1) -> str:
    return "–" if not meters else f"{meters / 1000:.{decimals}f} km"


def pace_per_km(speed_mps: float | None) -> str | None:
    if not speed_mps or speed_mps <= 0:
        return None
    s = round(1000 / speed_mps)
    return f"{s // 60}:{s % 60:02d}/km"


def pace_per_100m(speed_mps: float | None) -> str | None:
    if not speed_mps or speed_mps <= 0:
        return None
    s = round(100 / speed_mps)
    return f"{s // 60}:{s % 60:02d}/100m"


def num(v: float | None, decimals: int = 0, signed: bool = False) -> str:
    if v is None:
        return "–"
    s = f"{v:+.{decimals}f}" if signed else f"{v:.{decimals}f}"
    return s


def pct(v: float | None, signed: bool = True) -> str:
    return "–" if v is None else (f"{v:+.0%}" if signed else f"{v:.0%}")


def age_label(minutes: int | None) -> str:
    if minutes is None:
        return "mai"
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} g"


def approx_tokens(text: str) -> int:
    """Conservative token estimate for Italian Markdown (~3.5 characters per token)."""
    return round(len(text) / 3.5)
