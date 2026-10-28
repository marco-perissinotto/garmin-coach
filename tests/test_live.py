"""The only test that touches the network. Opt-in: GARMIN_COACH_LIVE=1 uv run pytest tests/test_live.py"""

from __future__ import annotations

import os

import pytest

from garmin_coach.garmin import endpoints as ep
from garmin_coach.garmin.client import GarminClient

pytestmark = pytest.mark.skipif(os.environ.get("GARMIN_COACH_LIVE") != "1", reason="set GARMIN_COACH_LIVE=1")


def test_token_profile_and_one_activity() -> None:
    c = GarminClient(budget=3, min_gap_s=0.5)
    try:
        settings = c.fetch(ep.get("profile.settings"), {})
        assert isinstance(settings, dict) and "measurementSystem" in settings
        page = c.fetch(ep.get("activities.search"), {"start": 0, "limit": 1})
        assert isinstance(page, list) and len(page) <= 1
    finally:
        c.close()
