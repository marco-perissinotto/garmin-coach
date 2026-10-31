"""Offline fakes: a synthetic Garmin account built from the fixture templates, and a controllable clock."""

from __future__ import annotations

import copy
import json
import random
import threading
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import fitgen

from garmin_coach.garmin.chunking import parse_range_key
from garmin_coach.garmin.client import BudgetExhaustedError, NotFoundError
from garmin_coach.garmin.endpoints import Endpoint

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.now

    def advance(self, **kw: float) -> None:
        with self._lock:
            self.now += timedelta(**kw)


def _days(start: str, end: str) -> list[str]:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


class FakeGarmin:
    """Implements the Fetcher protocol over a deterministic synthetic account.

    ``errors`` maps endpoint name -> exception instance or callable(args) raising; ``every_call`` hooks run first.
    """

    def __init__(self, today: date = date(2026, 10, 28), n_activities: int = 150, budget: int = 10_000) -> None:
        self.today = today
        self.budget = budget
        self.calls_made = 0
        self.log: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, Any] = {}
        self.no_data_days: set[str] = set()
        self.no_fit_ids: set[int] = set()  # activity.download -> 404 (manual entries, no original file)
        self.bad_zip_ids: set[int] = set()  # activity.download -> random bytes
        self.gpx_ids: set[int] = set()  # activity.download -> a ZIP holding a .gpx
        self.version = 0  # bump to make every response differ (simulates Garmin revising data)
        self.lock = threading.Lock()
        tmpl = fixture("activity_summary")
        self.activities = []
        for i in range(n_activities):  # roughly one every 2.4 days over the last year, newest first
            start = datetime(today.year, today.month, today.day, 7, 0) - timedelta(hours=58 * i + 10)
            a = copy.deepcopy(tmpl)
            a["activityId"] = 2_000_000 - i
            a["activityName"] = f"Activity {i}"
            a["startTimeLocal"] = start.strftime("%Y-%m-%d %H:%M:%S")
            a["startTimeGMT"] = (start - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            a["activityTrainingLoad"] = 50.0 + (i % 7) * 20
            self.activities.append(a)

    def close(self) -> None:
        pass

    def counts(self) -> Counter[str]:
        return Counter(name for name, _ in self.log)

    # -- Fetcher --------------------------------------------------------------------------------------------------
    def fetch(self, endpoint: Endpoint, args: dict[str, Any]) -> Any:
        with self.lock:
            if self.calls_made >= self.budget:
                raise BudgetExhaustedError("fake budget exhausted")
            self.calls_made += 1
            self.log.append((endpoint.name, dict(args)))
        err = self.errors.get(endpoint.name) or self.errors.get("*")
        if err is not None:
            if callable(err) and not isinstance(err, BaseException):
                err(args)
            else:
                raise err
        return getattr(self, "_" + endpoint.name.replace(".", "_"))(args)

    # -- endpoint emulation ---------------------------------------------------------------------------------------
    def _v(self, x: Any) -> Any:
        if isinstance(x, dict) and self.version:
            x = {**x, "_v": self.version}
        return x

    def _activities_search(self, a: dict[str, Any]) -> list[Any]:
        rows = self.activities
        if a.get("start_date"):
            rows = [r for r in rows if a["start_date"] <= r["startTimeLocal"][:10] <= a.get("end_date", "9999")]
        s, n = int(a.get("start", 0)), int(a.get("limit", 20))
        return [self._v(r) for r in rows[s : s + n]]

    def _activity_detail(self, a: dict[str, Any]) -> Any:
        return {"activityId": a["activity_id"], "summaryDTO": {"distance": 1000.0}, "splitSummaries": []}

    def _activity_workouts(self, a: dict[str, Any]) -> Any:
        return []

    def _activity_download(self, a: dict[str, Any]) -> bytes:
        aid = int(a["activity_id"])
        if aid in self.no_fit_ids:
            raise NotFoundError("activity.download: HTTP 404", 404)
        if aid in self.bad_zip_ids:
            return random.Random(aid).randbytes(2000)
        if aid in self.gpx_ids:
            return fitgen.zip_bytes(f"{aid}.gpx", fitgen.GPX)
        return fitgen.activity_zip(aid)

    def _day_or_404(self, d: str) -> None:
        if d in self.no_data_days:
            raise NotFoundError("fake 404", 404)

    def _usersummary_daily(self, a: dict[str, Any]) -> Any:
        self._day_or_404(a["date"])
        return self._v({**fixture("usersummary_daily"), "calendarDate": a["date"]})

    def _usersummary_range(self, a: dict[str, Any]) -> Any:
        t = fixture("usersummary_range")["data"][0]
        return {"data": [self._v({**t, "calendarDate": d}) for d in _days(a["start"], a["end"])]}

    def _sleep_daily(self, a: dict[str, Any]) -> Any:
        self._day_or_404(a["date"])
        return self._v({**fixture("sleep_daily"), "calendarDate": a["date"]})

    def _sleep_range(self, a: dict[str, Any]) -> Any:
        return [self._v({**fixture("sleep_daily"), "calendarDate": d}) for d in _days(a["start"], a["end"])]

    def _hrv_daily(self, a: dict[str, Any]) -> Any:
        self._day_or_404(a["date"])
        f = fixture("hrv_daily")
        f["hrvSummary"]["calendarDate"] = a["date"]
        return self._v(f)

    def _hrv_range(self, a: dict[str, Any]) -> Any:
        t = fixture("hrv_range")["hrvSummaries"][0]
        return self._v({"hrvSummaries": [{**t, "calendarDate": d} for d in _days(a["start"], a["end"])]})

    def _hr_daily(self, a: dict[str, Any]) -> Any:
        self._day_or_404(a["date"])
        return self._v({**fixture("hr_daily"), "calendarDate": a["date"]})

    def _bodybattery_range(self, a: dict[str, Any]) -> Any:
        t = fixture("bodybattery_range")[0]
        return [self._v({**t, "date": d}) for d in _days(a["start"], a["end"])]

    def _stress_range(self, a: dict[str, Any]) -> Any:
        t = fixture("stress_range")[0]
        return [self._v({**t, "calendarDate": d}) for d in _days(a["start"], a["end"])]

    def _steps_range(self, a: dict[str, Any]) -> Any:
        t = fixture("steps_range")[0]
        return [self._v({**t, "calendarDate": d}) for d in _days(a["start"], a["end"])]

    def _hydration_range(self, a: dict[str, Any]) -> Any:
        t = fixture("hydration_range")[0]
        return [self._v({**t, "calendarDate": d}) for d in _days(a["start"], a["end"])]

    def _wellnessactivity_range(self, a: dict[str, Any]) -> Any:
        return []

    def _weigh_ins(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        t = fixture("weight_all")[0]
        out = []
        for d in _days(a["start"], a["end"]):
            if date.fromisoformat(d).weekday() == 0:
                ms = int(datetime.fromisoformat(d).replace(hour=6, tzinfo=UTC).timestamp() * 1000)
                out.append({**t, "calendarDate": d, "timestampGMT": ms})
        return out

    def _weight_all(self, a: dict[str, Any]) -> Any:
        return self._weigh_ins(a)

    def _weight_range(self, a: dict[str, Any]) -> Any:
        return {"dailyWeightSummaries": [{"summaryDate": w["calendarDate"], "allWeightMetrics": [w]}
                                         for w in self._weigh_ins(a)]}

    def _weightgoal_range(self, a: dict[str, Any]) -> Any:
        return {"userGoals": [], "userGoalRanges": []}

    def _hrzones(self, a: dict[str, Any]) -> Any:
        return fixture("hrzones")

    def _fitnessage_daily(self, a: dict[str, Any]) -> Any:
        return [{"chronologicalAge": 45, "fitnessAge": 38.0}]

    def _profile_settings(self, a: dict[str, Any]) -> Any:
        return fixture("profile_settings")

    def _spo2_daily(self, a: dict[str, Any]) -> Any:
        return {"calendarDate": a["date"]}


def range_days(key: str) -> int:
    a, b = parse_range_key(key)
    return (b - a).days + 1
