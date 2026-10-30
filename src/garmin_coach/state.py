"""The short-term coaching state: ``coaching-state.yaml`` in the config folder (PLAN §11.3, D-060..D-066).

Only what is true *now*: symptoms the athlete reported, the plan agreed for the next days and the decisions open or
taken. Overwritten per section by ``update_coaching_state`` (or by hand), never appended to; durable rules go to
``coach-notes.md`` and Garmin values stay in the cache. Pure module: no MCP imports, no network.

Loading never raises: a missing file is ``None`` with no problem (the state is optional), an invalid one is ``None``
plus a problem string. Saving validates, fills the defaults, redacts and writes atomically.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterable
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from .secrets import redact
from .settings import STATE_FILE, Paths

SECTIONS = ("symptoms", "plan", "decisions")
MIN_TEXT_CHARS = 3
MAX_TEXT_CHARS = 300
MAX_SYMPTOMS = 8
MAX_PLAN_DAYS = 14
MAX_CHANGE_IF = 8
MAX_OPEN = 8
MAX_TAKEN = 10
MAX_STATE_CHARS = 4000  # whole file, serialized (~1 000 tokens): keeps the daily brief inside its budget
PAST_DAYS = 7  # plan days, plan.to and decide_by may lie at most this far in the past ...
FUTURE_DAYS = 90  # ... and at most this far in the future
HEADER = (
    "# coaching-state.yaml: the CURRENT short-term coaching state, overwritten by the coach (update_coaching_state)\n"
    "# or by hand. Not a diary: only what is true now. Durable rules go to coach-notes.md, Garmin data stays in Garmin.\n"
    "# Rewritten as a whole on every update: comments other than this header are not kept.\n"
)
_ITEM_HINTS = {"symptoms": "drop resolved ones", "plan.days": "keep at most the next two weeks",
               "plan.change_if": "keep the main ones", "decisions.open": "drop the ones already decided",
               "decisions.taken": "drop the oldest ones"}


class StateError(ValueError):
    """The state breaks a limit or a date rule; the message is written for the model."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", validate_by_name=True, validate_by_alias=True)


class Symptom(_Model):
    text: str
    since: dt.date | None = None
    noted: dt.date | None = None


class PlanDay(_Model):
    date: dt.date
    session: str


class Plan(_Model):
    from_: dt.date | None = Field(default=None, alias="from")
    to: dt.date | None = None
    days: list[PlanDay] = Field(default_factory=list)
    change_if: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.days and not self.change_if

    def end(self) -> dt.date | None:
        return self.to or max((d.date for d in self.days), default=None)


class OpenDecision(_Model):
    question: str
    decide_by: dt.date | None = None
    noted: dt.date | None = None


class TakenDecision(_Model):
    text: str
    date: dt.date | None = None


class Decisions(_Model):
    open: list[OpenDecision] = Field(default_factory=list)
    taken: list[TakenDecision] = Field(default_factory=list)


class CoachingState(_Model):
    updated: dt.datetime | None = None
    updated_by: str | None = None
    symptoms: list[Symptom] = Field(default_factory=list)
    plan: Plan | None = None
    decisions: Decisions = Field(default_factory=Decisions)
    _mtime: dt.datetime | None = PrivateAttr(default=None)  # file mtime, used when a hand edit has no `updated`


# -- validation ---------------------------------------------------------------------------------------------------
def _loc(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def validation_message(exc: ValidationError) -> str:
    """The first pydantic error as one sentence in the tool's wording (``plan.days[0].date must be ...``)."""
    err = exc.errors()[0]
    where = _loc(err["loc"]) or "state"
    if err["type"] == "missing":
        return f"{where} is required"
    if err["type"].startswith(("date", "datetime")):
        return f"{where} must be a date as YYYY-MM-DD"
    return f"{where}: {err['msg']}"


def _text(value: str, where: str) -> str:
    s = " ".join(redact(value).split())
    if len(s) < MIN_TEXT_CHARS:
        raise StateError(f"{where} is too short ({len(s)} characters, min {MIN_TEXT_CHARS})")
    if len(s) > MAX_TEXT_CHARS:
        raise StateError(f"{where} is too long ({len(s)} characters, max {MAX_TEXT_CHARS}): summarise it")
    return s


def _count(items: list[Any], where: str, limit: int) -> None:
    if len(items) > limit:
        raise StateError(f"{where}: {len(items)} items, max {limit}: {_ITEM_HINTS[where]}")


def _not_future(d: dt.date | None, where: str, today: dt.date | None) -> None:
    if d is not None and today is not None and d > today:
        raise StateError(f"{where} must not be in the future")


def _window(d: dt.date | None, where: str, today: dt.date | None, past: bool) -> None:
    if d is None or today is None:
        return
    if d > today + dt.timedelta(days=FUTURE_DAYS):
        raise StateError(f"{where} must be within {FUTURE_DAYS} days from today")
    if past and d < today - dt.timedelta(days=PAST_DAYS):
        raise StateError(f"{where} must not be more than {PAST_DAYS} days in the past")


def check(state: CoachingState, today: dt.date | None, past_window: Iterable[str] = ()) -> None:
    """Collapse whitespace and redact every text, then enforce the limits and date rules (raises ``StateError``).

    ``today`` None skips the date rules. The lower bound of the date window (today - 7) is enforced only for the
    sections in ``past_window``, the ones being written now: a state kept or read back must not turn invalid just
    because time passed (an old state is flagged stale, never hidden, D-063)."""
    past = set(past_window)
    _count(state.symptoms, "symptoms", MAX_SYMPTOMS)
    for i, s in enumerate(state.symptoms):
        s.text = _text(s.text, f"symptoms[{i}].text")
        _not_future(s.since, f"symptoms[{i}].since", today)
        _not_future(s.noted, f"symptoms[{i}].noted", today)
    if state.plan is not None:
        p = state.plan
        _count(p.days, "plan.days", MAX_PLAN_DAYS)
        _count(p.change_if, "plan.change_if", MAX_CHANGE_IF)
        for i, d in enumerate(p.days):
            d.session = _text(d.session, f"plan.days[{i}].session")
            _window(d.date, f"plan.days[{i}].date", today, "plan" in past)
        p.change_if = [_text(c, f"plan.change_if[{i}]") for i, c in enumerate(p.change_if)]
        _window(p.to, "plan.to", today, "plan" in past)
    dec = state.decisions
    _count(dec.open, "decisions.open", MAX_OPEN)
    _count(dec.taken, "decisions.taken", MAX_TAKEN)
    for i, o in enumerate(dec.open):
        o.question = _text(o.question, f"decisions.open[{i}].question")
        _window(o.decide_by, f"decisions.open[{i}].decide_by", today, "decisions" in past)
        _not_future(o.noted, f"decisions.open[{i}].noted", today)
    for i, t in enumerate(dec.taken):
        t.text = _text(t.text, f"decisions.taken[{i}].text")
        _not_future(t.date, f"decisions.taken[{i}].date", today)


# -- file ---------------------------------------------------------------------------------------------------------
class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True  # plan.to is often the same date object as the last plan day: no &id anchors in a hand file


_Dumper.add_representer(dt.datetime, lambda d, v: d.represent_scalar("tag:yaml.org,2002:timestamp", v.isoformat()))


def dump(state: CoachingState) -> str:
    """The file text: fixed header + YAML (empty plan left out, keys in schema order)."""
    data = state.model_dump(by_alias=True, exclude_none=True)
    if state.plan is None or state.plan.is_empty():
        data.pop("plan", None)
    body = yaml.dump(data, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=120)
    return HEADER + body


def load_state(paths: Paths, today: dt.date | None = None) -> tuple[CoachingState | None, list[str]]:
    """``(state, problems)``; never raises. ``today`` enables the date rules that cannot expire with time."""
    path = paths.config_dir / STATE_FILE
    try:
        if not path.exists():
            return None, []
        text = path.read_text(encoding="utf-8")
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC).astimezone()
        data = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return None, [f"{STATE_FILE} non valido ({type(exc).__name__})"]
    if data is None:
        return None, []  # empty file: no state
    if not isinstance(data, dict):
        return None, [f"{STATE_FILE} non valido (non è una mappa YAML)"]
    try:
        state = CoachingState.model_validate(data)
        check(state, today)
    except ValidationError as exc:
        return None, [f"{STATE_FILE} non valido ({validation_message(exc)})"]
    except StateError as exc:
        return None, [f"{STATE_FILE} non valido ({exc})"]
    if state.plan is not None and state.plan.is_empty():
        state.plan = None
    state._mtime = mtime
    return state, []


def save_state(paths: Paths, state: CoachingState, now: dt.datetime,
               past_window: Iterable[str] = SECTIONS) -> CoachingState:
    """Fill the defaults, validate (``StateError``), stamp ``updated`` and write the file atomically."""
    local_now = now.astimezone().replace(microsecond=0)
    today = local_now.date()
    for s in state.symptoms:
        s.noted = s.noted or today
    if state.plan is not None:
        if state.plan.is_empty():
            state.plan = None
        elif state.plan.to is None:
            state.plan.to = state.plan.end()
    for o in state.decisions.open:
        o.noted = o.noted or today
    for t in state.decisions.taken:
        t.date = t.date or today
    check(state, today, past_window)
    state.updated, state.updated_by = local_now, "coach"
    text = dump(state)
    if len(text) > MAX_STATE_CHARS:
        n, cap = f"{len(text):,}".replace(",", " "), f"{MAX_STATE_CHARS:,}".replace(",", " ")
        raise StateError(f"state too large ({n} characters, max {cap}): remove resolved items")
    path = paths.config_dir / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    state._mtime = local_now
    return state


# -- views --------------------------------------------------------------------------------------------------------
def content(state: CoachingState) -> dict[str, Any]:
    """The three sections as JSON-able data (dates as ISO strings)."""
    data = state.model_dump(mode="json", by_alias=True, exclude_none=True, include=set(SECTIONS))
    data.setdefault("plan", None)
    return data


def derived(state: CoachingState, today: dt.date, stale_after_days: int) -> dict[str, Any]:
    """Computed on read, never stored: age, staleness, expired plan, decisions whose deadline has come."""
    updated = state.updated or state._mtime
    by = state.updated_by if state.updated and state.updated_by else "owner"
    age = None if updated is None else max(0, (today - updated.astimezone().date()).days)
    end = state.plan.end() if state.plan else None
    return {
        "updated": None if updated is None else updated.isoformat(), "updated_by": by,
        "age_days": age, "stale": age is not None and age > stale_after_days, "stale_after_days": stale_after_days,
        "plan_expired": end is not None and end < today,
        "decisions_due": [o.model_dump(mode="json", exclude_none=True) for o in state.decisions.open
                          if o.decide_by is not None and o.decide_by <= today],
    }


def view(state: CoachingState | None, problems: list[str], today: dt.date, stale_after_days: int) -> dict[str, Any]:
    """The object returned by get_coaching_state and embedded in the daily brief (design §2.1)."""
    if state is None:
        out: dict[str, Any] = {"state": None}
        if problems:
            out["problems"] = problems
        else:
            out["note"] = "no coaching state recorded yet"
        return {**out, "file": STATE_FILE}
    return {"state": content(state), **derived(state, today, stale_after_days), "file": STATE_FILE}
