"""The owner's manual inputs: athlete.yaml, events.yaml, history.md, coach-notes.md and the optional
coaching-state.yaml (PLAN §11).

Loading never raises: a missing or invalid file becomes an entry in ``Inputs.problems`` so packs and tools can say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .settings import Paths
from .state import CoachingState, load_state

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class Athlete(_Model):
    name: str | None = None
    birth_year: int | None = None
    sex: str | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    weight_date: date | None = None
    sports: list[str] = Field(default_factory=list)
    max_hr: int | None = None
    lthr: int | None = None
    ftp: int | None = None
    ftp_date: date | None = None
    constraints: list[str] = Field(default_factory=list)
    availability: dict[str, int | None] = Field(default_factory=dict)
    availability_notes: str | None = None
    advice_language: str = "it"
    units: str = "metric"

    @field_validator("birth_year", mode="before")
    @classmethod
    def _year(cls, v: Any) -> Any:
        # A full birth date (YYYY-MM-DD) is common in the YAML; accept a date, an ISO string or a plain year.
        if isinstance(v, date | datetime):
            return v.year
        if isinstance(v, str) and len(v) >= 4 and v[:4].isdigit():
            return int(v[:4])
        return v

    @field_validator("sports", "constraints", mode="before")
    @classmethod
    def _list(cls, v: Any) -> Any:
        return [] if v is None else v

    @field_validator("availability", mode="before")
    @classmethod
    def _avail(cls, v: Any) -> Any:
        return {} if v is None else v

    def age_on(self, d: date) -> int | None:
        return None if self.birth_year is None else d.year - self.birth_year


class Event(_Model):
    name: str
    date: date
    sport: str | None = None
    distance_km: float | None = None
    elevation_m: float | None = None
    goal: str | None = None
    priority: str | None = None
    notes: str | None = None


@dataclass
class Inputs:
    athlete: Athlete = field(default_factory=Athlete)
    events: list[Event] = field(default_factory=list)
    history_md: str | None = None
    coach_notes_md: str | None = None
    state: CoachingState | None = None
    problems: list[str] = field(default_factory=list)


def _read_yaml(path: Path, problems: list[str]) -> Any:
    if not path.exists():
        problems.append(f"{path.name} mancante")
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        problems.append(f"{path.name} non valido ({type(exc).__name__})")
        return None


def _read_text(path: Path, problems: list[str]) -> str | None:
    if not path.exists():
        problems.append(f"{path.name} mancante")
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def load_inputs(paths: Paths, today: date | None = None) -> Inputs:
    """``today`` (from the injectable clock) enables the coaching-state date rules that cannot expire with time."""
    inp = Inputs()
    cfg = paths.config_dir
    data = _read_yaml(cfg / "athlete.yaml", inp.problems)
    if isinstance(data, dict):
        try:
            inp.athlete = Athlete.model_validate(data)
        except ValidationError as exc:
            inp.problems.append(f"athlete.yaml: {exc.error_count()} campi non validi")
    data = _read_yaml(cfg / "events.yaml", inp.problems)
    raw_events = data.get("events") if isinstance(data, dict) else None
    for i, e in enumerate(raw_events or []):
        try:
            inp.events.append(Event.model_validate(e))
        except ValidationError:
            inp.problems.append(f"events.yaml: evento #{i + 1} non valido")
    inp.events.sort(key=lambda e: e.date)
    inp.history_md = _read_text(cfg / "history.md", inp.problems)
    inp.coach_notes_md = _read_text(cfg / "coach-notes.md", inp.problems)
    inp.state, state_problems = load_state(paths, today)
    inp.problems += state_problems
    return inp
