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


def test_fit_download_parses_and_zones_match_the_list() -> None:
    """PLAN-FIT §11/§12: 2 calls. Prints what §12 asks to record (ZIP member, timing, zone index alignment)."""
    import io
    import time
    import zipfile

    from garmin_coach.fit import parse

    c = GarminClient(budget=2, min_gap_s=1.0)
    try:
        page = c.fetch(ep.get("activities.search"), {"start": 0, "limit": 10})
        act = next((a for a in page if a.get("hrTimeInZone_1") is not None), None)
        assert act is not None, "no recent activity with HR zones in the list"
        data = c.fetch(ep.get("activity.download"), {"activity_id": act["activityId"]})
    finally:
        c.close()
    assert isinstance(data, bytes)
    members = zipfile.ZipFile(io.BytesIO(data)).namelist() if zipfile.is_zipfile(io.BytesIO(data)) else ["<bare>"]
    t0 = time.perf_counter()
    res = parse.parse_archive(data)
    seconds = time.perf_counter() - t0
    assert res.status == "ok", res.error
    s = res.summary or {}
    hr = s.get("zones", {}).get("hr", {}).get("time_s") or []
    garmin = [round(act.get(f"hrTimeInZone_{i}") or 0) for i in range(1, 6)]
    print(f"\nmembers={members} zip_bytes={len(data)} fit_bytes={res.fit_bytes} records={res.n_records} "
          f"parse_s={seconds:.2f} type={act.get('activityType', {}).get('typeKey')}")
    print(f"zones source={s.get('zones', {}).get('source')} fit={hr} list_1..5={garmin}")
    print(f"session tss={s['session'].get('tss')} te={s['session'].get('aerobic_te')} "
          f"load_peak={s['session'].get('training_load_peak')} | list load={act.get('activityTrainingLoad')} "
          f"te={act.get('aerobicTrainingEffect')}")
    print(f"lap balance={[x.get('balance_right_pct') for x in s.get('laps', [])][:5]} rpe={s['session'].get('rpe')}")

    def close(a: list[int]) -> bool:
        return len(a) == 5 and all(abs(x - y) <= 2 for x, y in zip(a, garmin, strict=True))

    # Either index 0 is "below zone 1" (Garmin's zones 1-5 = indexes 1-5) or the array starts at zone 1.
    assert close(hr[1:6]) or close(hr[0:5]), "FIT time in zones does not match the activity list"
