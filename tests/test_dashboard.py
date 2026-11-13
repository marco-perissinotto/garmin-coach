"""Dashboard: numbers, sentences and the HTML page, against the fake account. No network, no browser."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeClock
from test_context import full  # noqa: F401 - fixture: fake account + inputs, history and tiers loaded
from test_mcp_tools import _key_present, warmed  # noqa: F401 - fixtures: key file present (autouse), warmed service

from garmin_coach.athlete import load_inputs
from garmin_coach.dashboard import build, data, insights
from garmin_coach.mcp.tools import CoachService
from garmin_coach.settings import Paths, Settings

TODAY = date(2026, 10, 28)  # the fake clock's day


# -- pure parts -----------------------------------------------------------------------------------------------------
def test_split_domains_shares_a_crossed_bin_in_proportion() -> None:
    # bins: <100, 100-120, 120-140, 140-160, >160; thresholds 130 and 150 cut the 3rd and 4th bins in half
    low, mid, high = data.split_domains([10, 20, 40, 60, 30], [100, 120, 140, 160], 130, 150)
    assert (low, mid, high) == (50, 50, 60)
    assert data.split_domains([0, 0, 0], [100, 120], 300, 400) == (0, 0, 0)


def test_trend_compares_medians_and_reads_direction() -> None:
    pts = [(TODAY - timedelta(days=d), 1.0) for d in range(50, 130, 10)]  # the 3 months before: 1.0
    pts += [(TODAY - timedelta(days=d), 1.1) for d in range(0, 40, 10)]  # the last 6 weeks: 1.1
    t = data.trend(pts, TODAY)
    assert t["verdict"] == "up" and round(t["change_pct"]) == 10 and t["n_recent"] == 4
    assert data.trend(pts, TODAY, higher_is_better=False)["verdict"] == "down"
    assert data.trend(pts[:2], TODAY)["verdict"] is None  # too few sessions: no verdict


def test_italian_number_formatting() -> None:
    assert insights.n_it(1234.5, 1) == "1 234,5"
    assert insights.n_it(-4.5, 1, True) == "−4,5" and insights.pct_it(3.2) == "+3%"
    assert insights.hms(5629) == "1:33:49" and insights.pace_km(315) == "5:15/km"


def _payload(**over: Any) -> dict[str, Any]:
    """A minimal payload: every block the insights read, empty or neutral."""
    flat = {"verdict": None, "recent": None, "previous": None, "n_recent": 0, "change_pct": None, "yoy_pct": None,
            "better_pct": None}
    comp = {"recent": None, "previous": None, "n_recent": 0, "n_previous": 0, "delta": None}
    vol = {"km_7": 0, "km_week_before": 0, "ramp_pct": None}
    d = {"today": TODAY.isoformat(), "target": None, "recent_race": False, "state": None, "event": None,
         "intensity": None, "hero": None,
         "run": {"efficiency": {"trend": flat}, "easy_pace": {"recent": None}, "bests": [], "dynamics": [],
                 "decoupling": comp, "volume": vol},
         "bike": {"efficiency_outdoor": {"trend": flat}, "efficiency_indoor": {"trend": flat}, "power": [],
                  "ftp": {}, "decoupling": comp},
         "swim": {"pace": {"trend": flat}, "swolf": comp},
         "health": {"metrics": [], "sleep": None, "alarm": None},
         "strength": {"sessions_28": 0, "sessions_90": 0, "last": None},
         "consistency": {"active_days": 10, "days": 28}, "patterns": {}, "years": {"years": []}}
    d.update(over)
    return d


def test_insights_name_the_numbers_and_rank_the_alarms_first() -> None:
    hero = {"ctl": 90.0, "ctl_28": 97.0, "ctl_365": 104.0, "acwr": 1.7, "monotony": 1.0, "form": 0.04, "tsb": 3.0,
            "rank": 0.76, "history_from": "2016-05-30", "readiness": {"score": 96}}
    d = _payload(hero=hero, recent_race=True)
    d["bike"]["ftp"] = {"set": 245, "estimate": 216, "from_w": 227, "date": "2026-09-10"}
    d["health"]["alarm"] = {"rhr_base": 40, "rhr": 47, "hrv": 60}
    out = insights.build(d)
    g = out["groups"]
    assert g["watch"][0]["title"].startswith("FC a riposo alta")  # the strongest signal leads
    assert any("ACWR" in i["title"] or "Picco" in i["title"] for i in g["watch"])
    ftp = next(i for i in g["focus"] if i["title"] == "Verifica l'FTP")
    assert "245 W" in ftp["text"] and "216 W" in ftp["text"]
    assert any(i["title"] == "Forma in calo" and "Dopo una gara" in i["text"] for i in g["down"])
    assert all(len(v) <= insights.MAX_PER_GROUP for v in g.values())
    assert out["summary"].startswith("Forma 90: più alta del 76% dei tuoi giorni dal 2016")
    assert "Da tenere d'occhio" in out["summary"]


def test_insights_without_data_still_answer() -> None:
    out = insights.build(_payload())
    assert all(v == [] for v in out["groups"].values()) and out["patterns"] == []
    assert "backfill" in out["summary"]


def test_event_gap_points_at_the_discipline_behind_past_seasons() -> None:
    same = {"ctl": 85.0, "h_bike": 5.0, "h_run": 1.3, "h_swim": 1.5, "run_km": 15.0}
    cur = {"ctl": 90.0, "h_bike": 2.0, "h_run": 2.1, "h_swim": 1.4, "run_km": 25.0}
    ev = {"event": "Test 70.3", "days": 160, "sport": "triathlon",
          "series": [{"label": "2025", "current": False, "same_point": same},
                     {"label": "2027", "current": True, "same_point": cur}]}
    g = insights.build(_payload(event=ev))["groups"]
    assert any("parti più avanti" in i["title"] for i in g["strong"])
    focus = next(i for i in g["focus"] if "margine" in i["title"])
    assert focus["area"] == "Bici" and "2,0 h" in focus["text"] and "52%" in focus["text"]


# -- against the fake account -----------------------------------------------------------------------------------------
def _page_data(html: str) -> dict[str, Any]:
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
    assert m
    return json.loads(m.group(1))


def test_payload_and_page_from_the_fake_account(full, paths: Paths, settings: Settings,  # noqa: F811
                                               clock: FakeClock) -> None:
    full.execute("UPDATE activity SET name = ? WHERE activity_id = (SELECT MAX(activity_id) FROM activity)",
                 ("</script><img src=x onerror=alert(1)> & co",))
    d = build.payload(full, paths, settings, clock=clock)
    assert d["today"] == "2026-10-28" and d["hero"]["ctl"] > 0
    assert len(d["series"]["dates"]) == data.HISTORY_DAYS and d["series"]["dates"][-1] == "2026-10-28"
    assert len(d["weekly"]["weeks"]) == data.WEEKS
    assert set(d["insights"]["groups"]) == set(insights.GROUPS) and d["insights"]["summary"]
    assert d["target"]["name"] == "Test 70.3" and d["events"][0]["days"] == 11  # the A event, not the first one
    assert d["state"] is None and d["recent"]
    # CTL per sport adds up to the total CTL
    s = d["series"]
    total = sum(s[f"ctl_{k}"][-1] for k in data.KEYS)
    assert abs(total - s["ctl"][-1]) < 0.5

    path = build.build(full, paths, settings, clock=clock)
    assert path == build.page_path(paths) and path.parent == paths.home / "dashboard"
    html = path.read_text(encoding="utf-8")
    assert "</script><img" not in html  # untrusted text cannot close the data block
    assert _page_data(html)["recent"][0]["name"].startswith("</script>")
    assert "cdn" not in html and "http://" not in html.replace("http://www.w3.org/2000/svg", "")  # self-contained


def test_page_script_parses(full, paths: Paths, settings: Settings, clock: FakeClock,  # noqa: F811
                            tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    html = build.build(full, paths, settings, clock=clock).read_text(encoding="utf-8")
    script = html.split("<script>")[1].split("</script>")[0]
    (tmp_path / "page.js").write_text(script, encoding="utf-8")
    r = subprocess.run([node, "--check", str(tmp_path / "page.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_after_sync_only_updates_an_existing_page(full, paths: Paths, settings: Settings,  # noqa: F811
                                                  clock: FakeClock) -> None:
    page = build.page_path(paths)
    build.after_sync(full, paths, settings, clock)
    assert not page.exists()  # never created behind the owner's back
    build.build(full, paths, settings, clock=clock)
    page.write_text("old", encoding="utf-8")
    settings.dashboard.auto_update = False
    build.after_sync(full, paths, settings, clock)
    assert page.read_text(encoding="utf-8") == "old"
    settings.dashboard.auto_update = True
    build.after_sync(full, paths, settings, clock)
    assert page.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_coaching_state_reaches_the_page(full, paths: Paths, settings: Settings, clock: FakeClock) -> None:  # noqa: F811
    from test_context import write_state

    write_state(paths)
    d = build.payload(full, paths, settings, clock=clock)
    assert d["state"]["plan"]["days"][0]["session"].startswith("corsa facile")
    assert any(i["area"] == "Sintomi" for i in d["insights"]["groups"]["watch"])


def test_open_dashboard_tool(warmed, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    svc: CoachService = warmed[0]
    opened: list[Path] = []
    monkeypatch.setattr(build, "open_in_browser", lambda p: opened.append(p) or True)
    r = svc.open_dashboard(open_browser=False)
    assert "error" not in r, r
    assert Path(r["path"]).exists() and r["url"].startswith("file:") and r["opened"] is False and not opened
    assert r["summary"] and set(r["insights"]) == set(insights.GROUPS) and "freshness" in r
    assert svc.open_dashboard()["opened"] is True and opened


def test_inputs_without_events_or_history(paths: Paths, settings: Settings, conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    d = build.payload(conn, paths, settings, clock=clock)
    assert d["hero"] is None and d["event"] is None and d["target"] is None and d["recent"] == []
    assert "backfill" in d["insights"]["summary"]
    html = build.render(d)
    assert _page_data(html)["hero"] is None
    assert load_inputs(paths).problems  # the page shows them
