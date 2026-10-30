"""The coaching state file (state.py): round trip, loading, defaults, limits, derived fields, atomic write. Offline."""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

import pytest
from fakes import FakeClock

from garmin_coach import state as state_m
from garmin_coach.settings import STATE_FILE, Paths
from garmin_coach.state import CoachingState, StateError, content, derived, load_state, save_state


def _today(clock: FakeClock) -> date:
    return clock().astimezone().date()


def sample(today: date) -> dict[str, Any]:
    return {
        "symptoms": [{"text": "fastidio al polpaccio destro,\n  in calo", "since": today - timedelta(days=2)}],
        "plan": {"from": today + timedelta(days=1),
                 "days": [{"date": today + timedelta(days=1), "session": "corsa facile 40' in Z2"},
                          {"date": today + timedelta(days=4), "session": "lungo 80' solo senza dolore"}],
                 "change_if": ["dolore sopra 3/10 in corsa: passare alla bici"]},
        "decisions": {"open": [{"question": "gara di prova, sì o no", "decide_by": today + timedelta(days=7)}],
                      "taken": [{"text": "alla mezza con le scarpe da allenamento"}]},
    }


def save(paths: Paths, data: dict[str, Any], clock: FakeClock) -> CoachingState:
    return save_state(paths, CoachingState.model_validate(data), clock())


# -- 1. round trip ----------------------------------------------------------------------------------------------------
def test_round_trip(paths: Paths, clock: FakeClock) -> None:
    saved = save(paths, sample(_today(clock)), clock)
    loaded, problems = load_state(paths, _today(clock))
    assert problems == [] and loaded is not None
    assert content(loaded) == content(saved)
    assert loaded.symptoms[0].text == "fastidio al polpaccio destro, in calo"  # whitespace collapsed
    local = clock().astimezone()
    assert loaded.updated == local and loaded.updated is not None
    assert loaded.updated.utcoffset() == local.utcoffset()  # local time, with its offset
    assert loaded.updated_by == "coach"


# -- 2. loading never raises -----------------------------------------------------------------------------------------
def test_load_missing_invalid_and_bad_date(paths: Paths, clock: FakeClock) -> None:
    today = _today(clock)
    assert load_state(paths, today) == (None, [])
    f = paths.config_dir / STATE_FILE
    f.write_text("symptoms: [unclosed", encoding="utf-8")
    state, problems = load_state(paths, today)
    assert state is None and len(problems) == 1 and problems[0].startswith(f"{STATE_FILE} non valido (")
    f.write_text("symptoms:\n  - {text: dolore al ginocchio, since: ieri}\n", encoding="utf-8")
    state, problems = load_state(paths, today)
    assert state is None and problems == [f"{STATE_FILE} non valido (symptoms[0].since must be a date as YYYY-MM-DD)"]
    f.write_text("- just a list\n", encoding="utf-8")
    assert load_state(paths, today)[0] is None and len(load_state(paths, today)[1]) == 1
    f.write_text(f"symptoms:\n  - {{text: dolore al ginocchio, since: {today + timedelta(days=1)}}}\n",
                 encoding="utf-8")
    assert "must not be in the future" in load_state(paths, today)[1][0]
    f.write_text("", encoding="utf-8")
    assert load_state(paths, today) == (None, [])


def test_old_state_stays_readable(paths: Paths, clock: FakeClock) -> None:
    """The lower bound of the date window is a write rule: a state does not turn invalid because time passed."""
    save(paths, sample(_today(clock)), clock)
    state, problems = load_state(paths, _today(clock) + timedelta(days=60))
    assert state is not None and problems == []


# -- 3. defaults -------------------------------------------------------------------------------------------------------
def test_defaults_filled(paths: Paths, clock: FakeClock) -> None:
    today = _today(clock)
    s = save(paths, sample(today), clock)
    assert s.symptoms[0].noted == today and s.decisions.open[0].noted == today
    assert s.decisions.taken[0].date == today
    assert s.plan is not None and s.plan.to == today + timedelta(days=4)  # the last plan day
    empty = save(paths, {"plan": {"days": [], "change_if": []}}, clock)
    assert empty.plan is None  # a plan with neither days nor conditions is absent


# -- 4. limits ---------------------------------------------------------------------------------------------------------
def _limit_cases(today: date) -> list[tuple[dict[str, Any], str]]:
    return [
        ({"symptoms": [{"text": f"sintomo numero {i}"} for i in range(9)]},
         "symptoms: 9 items, max 8: drop resolved ones"),
        ({"plan": {"days": [{"date": today, "session": "x" * 301}]}},
         "plan.days[0].session is too long (301 characters, max 300): summarise it"),
        ({"symptoms": [{"text": " ok "}]}, "symptoms[0].text is too short (2 characters, min 3)"),
        ({"decisions": {"open": [{"question": "gara sì o no", "decide_by": today + timedelta(days=91)}]}},
         "decisions.open[0].decide_by must be within 90 days from today"),
        ({"symptoms": [{"text": "dolore al ginocchio", "since": today + timedelta(days=1)}]},
         "symptoms[0].since must not be in the future"),
        ({"plan": {"days": [{"date": today - timedelta(days=8), "session": "corsa facile"}]}},
         "plan.days[0].date must not be more than 7 days in the past"),
        ({"symptoms": [{"text": "s" * 300}] * 8, "plan": {"days": [{"date": today, "session": "p" * 300}] * 6}},
         "state too large ("),
    ]


def test_limits(paths: Paths, clock: FakeClock) -> None:
    for data, message in _limit_cases(_today(clock)):
        with pytest.raises(StateError) as exc:
            save(paths, data, clock)
        assert str(exc.value).startswith(message), (str(exc.value), message)
    assert not (paths.config_dir / STATE_FILE).exists()  # nothing written by a refused state


def test_size_message_format(paths: Paths, clock: FakeClock) -> None:
    data = {"symptoms": [{"text": "s" * 300}] * 8, "plan": {"days": [{"date": _today(clock), "session": "p" * 300}] * 6}}
    with pytest.raises(StateError, match=r"^state too large \(\d \d{3} characters, max 4 000\): remove resolved items$"):
        save(paths, data, clock)


# -- 5. derived fields -------------------------------------------------------------------------------------------------
def test_derived_fields(paths: Paths, clock: FakeClock) -> None:
    today = _today(clock)
    s = save(paths, sample(today), clock)
    d0 = derived(s, today, 7)
    assert d0["age_days"] == 0 and not d0["stale"] and not d0["plan_expired"] and d0["decisions_due"] == []
    assert d0["updated_by"] == "coach" and d0["stale_after_days"] == 7
    d7, d8 = derived(s, today + timedelta(days=7), 7), derived(s, today + timedelta(days=8), 7)
    assert d7["age_days"] == 7 and not d7["stale"]
    assert d8["age_days"] == 8 and d8["stale"]
    assert not derived(s, today + timedelta(days=4), 7)["plan_expired"]  # plan.to is today + 4
    assert derived(s, today + timedelta(days=5), 7)["plan_expired"]
    assert derived(s, today + timedelta(days=6), 7)["decisions_due"] == []
    assert [x["question"] for x in derived(s, today + timedelta(days=7), 7)["decisions_due"]] == [
        "gara di prova, sì o no"]


def test_mtime_fallback_without_updated(paths: Paths, clock: FakeClock) -> None:
    f = paths.config_dir / STATE_FILE
    f.write_text("symptoms:\n  - text: dolore al ginocchio\n", encoding="utf-8")
    three_days_ago = (clock() - timedelta(days=3)).timestamp()
    os.utime(f, (three_days_ago, three_days_ago))
    state, _ = load_state(paths, _today(clock))
    assert state is not None and state.updated is None
    d = derived(state, _today(clock), 7)
    assert d["age_days"] == 3 and d["updated_by"] == "owner" and d["updated"]


# -- 6. atomic write ---------------------------------------------------------------------------------------------------
def test_atomic_write_and_header(paths: Paths, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    f = paths.config_dir / STATE_FILE
    f.write_text("# my own comment\nsymptoms:\n  - text: dolore al ginocchio  # inline note\n", encoding="utf-8")
    state, _ = load_state(paths, _today(clock))
    assert state is not None
    save_state(paths, state, clock())
    text = f.read_text(encoding="utf-8")
    assert text.startswith(state_m.HEADER) and "my own comment" not in text and "inline note" not in text
    assert "&id" not in text and "\r" not in f.read_bytes().decode("utf-8")
    assert not (paths.config_dir / f"{STATE_FILE}.tmp").exists()

    before = f.read_bytes()

    def boom(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(state_m.os, "replace", boom)
    with pytest.raises(OSError):
        save(paths, sample(_today(clock)), clock)
    assert f.read_bytes() == before and not (paths.config_dir / f"{STATE_FILE}.tmp").exists()


# -- 7. redaction ------------------------------------------------------------------------------------------------------
def test_redaction_on_disk(paths: Paths, clock: FakeClock) -> None:
    secret = "GCDC" + "_abcdefghijkl1234"
    save(paths, {"plan": {"days": [{"date": _today(clock), "session": f"corsa facile, chiave {secret}"}]}}, clock)
    text = (paths.config_dir / STATE_FILE).read_text(encoding="utf-8")
    assert secret not in text and "***REDACTED***" in text
