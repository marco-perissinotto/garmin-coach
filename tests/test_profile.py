"""Shared profile folder (profile.py, D-085..D-089) and the key expiry warning. Offline: the "sync client" is a
plain folder that the tests rename away to simulate a machine where it is not running."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fakes import FakeClock
from test_context import write_inputs
from test_mcp_tools import Fetchers, make_app, warm
from test_startup_jobs import JobFetchers, manager
from typer.testing import CliRunner

from garmin_coach import cli, profile
from garmin_coach import state as state_m
from garmin_coach.athlete import load_inputs
from garmin_coach.mcp import tools as tools_mod
from garmin_coach.mcp.tools import CoachService
from garmin_coach.secrets import KEY_WARN_DAYS, KeyFileStatus, key_warning
from garmin_coach.settings import PROFILE_FILES, STATE_FILE, Paths, ProfileUnavailable, Settings, is_cloud_synced
from garmin_coach.status import build_status
from garmin_coach.store import db
from garmin_coach.sync.startup import startup_check

runner = CliRunner()
PLAN = {"days": [{"date": "2026-10-29", "session": "corsa facile 40'"}]}


@pytest.fixture
def drive(tmp_path: Path) -> Path:
    """The root of the fake sync client: exists, holds nothing."""
    d = tmp_path / "drive"
    d.mkdir()
    return d


@pytest.fixture
def linked(paths: Paths, drive: Path) -> Path:
    write_inputs(paths)
    profile.link(paths, drive / "coach")
    return drive / "coach"


def _offline(folder: Path) -> None:
    folder.rename(folder.with_name("gone"))


# -- 1. where the files live ------------------------------------------------------------------------------------------
def test_default_profile_is_local(paths: Paths) -> None:
    st = profile.status(paths)
    assert (st.path, st.shared, st.reachable, st.conflicts) == (paths.home / "config", False, True, ())
    assert paths.config_dir == paths.local_config_dir and st.problems() == []


def test_link_fills_an_empty_folder_and_moves_reads_and_writes(paths: Paths, drive: Path, clock: FakeClock) -> None:
    write_inputs(paths)
    local = {n: (paths.local_config_dir / n).read_bytes() for n in PROFILE_FILES if (paths.local_config_dir / n).exists()}
    plan = profile.link(paths, drive / "coach")
    target = drive / "coach"
    assert plan.will_copy and sorted(plan.in_local) == sorted(local)
    assert {n: (target / n).read_bytes() for n in local} == local
    assert paths.config_dir == target and paths.settings_file == target / "settings.yaml"
    assert all((paths.local_config_dir / n).exists() for n in local)  # the originals stay as a backup

    (target / "history.md").write_text("# Storia condivisa\n", encoding="utf-8")
    assert load_inputs(paths).history_md == "# Storia condivisa"
    state_m.save_state(paths, state_m.CoachingState.model_validate({"plan": PLAN}), clock())
    assert (target / STATE_FILE).exists() and not (paths.local_config_dir / STATE_FILE).exists()
    assert profile.status(paths).as_dict() == {"path": str(target), "shared": True, "reachable": True, "conflicts": []}


def test_link_uses_the_profile_already_in_the_folder(paths: Paths, drive: Path) -> None:
    """The second machine: the folder came down with the sync client, the local files are examples or older."""
    write_inputs(paths)
    target = drive / "coach"
    target.mkdir()
    (target / "athlete.yaml").write_text("name: Dal primo computer\n", encoding="utf-8")
    plan = profile.link(paths, target)
    assert not plan.will_copy and plan.in_target == ["athlete.yaml"]
    assert "athlete.yaml" in plan.shadowed and "events.yaml" in plan.shadowed
    assert (target / "athlete.yaml").read_text(encoding="utf-8") == "name: Dal primo computer\n"
    assert not (target / "events.yaml").exists()  # nothing is merged into an existing profile


def test_link_refuses_bad_targets(paths: Paths, drive: Path) -> None:
    (drive / "a-file").write_text("x", encoding="utf-8")
    for bad, why in ((paths.home / "elsewhere", "inside the app data dir"), (drive / "a-file", "not a folder"),
                     (drive / "missing" / "coach", "sync client running")):
        with pytest.raises(profile.ProfileError, match=why):
            profile.link(paths, bad)
    assert paths.shared_profile is None


# -- 2. the shared folder is not there --------------------------------------------------------------------------------
def test_unreachable_folder_is_reported_and_never_recreated(paths: Paths, linked: Path, settings: Settings,
                                                            clock: FakeClock) -> None:
    _offline(linked)
    st = profile.status(paths)
    assert st.shared and not st.reachable
    paths.ensure()
    inp = load_inputs(paths, clock().astimezone().date())
    assert len(inp.problems) == 2 and "non raggiungibile" in inp.problems[0]
    assert inp.problems[1].startswith(STATE_FILE) and inp.history_md is None

    with pytest.raises(ProfileUnavailable):
        state_m.save_state(paths, state_m.CoachingState.model_validate({"plan": PLAN}), clock())
    svc = CoachService(make_app(paths, settings, clock, Fetchers()))
    assert "not reachable" in svc.append_coach_note("Lungo il sabato")["error"]
    assert "not reachable" in svc.update_coaching_state(plan=PLAN)["error"]
    assert "not reachable" in svc.update_coaching_state(symptoms=[], plan=PLAN, decisions={})["error"]
    got = svc.get_coaching_state()
    assert got["state"] is None and "non disponibile" in got["problems"][0]
    assert not linked.exists()  # no second, empty profile where the sync client will mount the real one


def test_empty_cache_does_not_backfill_without_the_shared_settings(paths: Paths, linked: Path, settings: Settings,
                                                                   clock: FakeClock) -> None:
    _offline(linked)
    assert startup_check(manager(paths, settings, clock, JobFetchers()), settings, clock=clock) is None
    linked.with_name("gone").rename(linked)
    m = manager(paths, settings, clock, JobFetchers())
    jid = startup_check(m, settings, clock=clock)
    assert jid is not None
    m.wait(jid, timeout=60)


# -- 3. conflict copies -----------------------------------------------------------------------------------------------
def test_conflict_copies_are_detected_and_surfaced(paths: Paths, linked: Path, settings: Settings,
                                                   clock: FakeClock) -> None:
    copies = ["coaching-state (1).yaml", "history (conflicted copy 2026-11-13).md", "athlete-MACBOOK.yaml",
              "coach-notes 2.md"]
    others = ["athlete.example.yaml", "history.md.bak-2026-11-01", "notes.txt", "coaching-state.yaml.tmp"]
    for name in copies + others:
        (linked / name).write_text("x", encoding="utf-8")
    assert profile.conflict_copies(linked) == sorted(copies)
    assert profile.conflict_copies(linked / "nowhere") == []
    problems = load_inputs(paths).problems
    assert any("copie in conflitto" in p and "coaching-state (1).yaml" in p for p in problems)
    conn = db.connect(paths.db)
    try:
        assert build_status(conn, settings, clock, paths=paths)["profile"]["conflicts"] == sorted(copies)
    finally:
        conn.close()


# -- 4. unlink --------------------------------------------------------------------------------------------------------
def test_unlink_brings_the_shared_files_back(paths: Paths, linked: Path) -> None:
    (linked / "history.md").write_text("# Scritta sull'altro computer\n", encoding="utf-8")
    before = (paths.local_config_dir / "history.md").read_bytes()
    res = profile.unlink(paths, date(2026, 11, 13))
    assert paths.shared_profile is None and paths.config_dir == paths.local_config_dir
    assert (paths.local_config_dir / "history.md").read_text(encoding="utf-8") == "# Scritta sull'altro computer\n"
    assert res.backups == ["history.md.bak-2026-11-13"]
    assert (paths.local_config_dir / "history.md.bak-2026-11-13").read_bytes() == before
    assert (linked / "history.md").exists()  # the shared folder is left alone
    with pytest.raises(profile.ProfileError):
        profile.unlink(paths, date(2026, 11, 13))


def test_unlink_without_the_folder_only_drops_the_link(paths: Paths, linked: Path) -> None:
    _offline(linked)
    res = profile.unlink(paths, date(2026, 11, 13))
    assert res.copied == [] and paths.shared_profile is None and (paths.local_config_dir / "athlete.yaml").exists()


# -- 5. command line --------------------------------------------------------------------------------------------------
def test_cli_link_asks_before_filling_an_empty_folder(paths: Paths, drive: Path) -> None:
    write_inputs(paths)
    target = drive / "coach"
    no = runner.invoke(cli.app_cli, ["profile", "link", str(target)], input="n\n")
    assert no.exit_code == 1 and paths.shared_profile is None and not target.exists()
    yes = runner.invoke(cli.app_cli, ["profile", "link", str(target)], input="y\n")
    assert yes.exit_code == 0, yes.output
    assert paths.shared_profile == target and (target / "athlete.yaml").exists()

    (target / "events (1).yaml").write_text("x", encoding="utf-8")
    show = " ".join(runner.invoke(cli.app_cli, ["profile", "show"]).stdout.split())
    assert "shared between machines" in show and "events (1).yaml" in show and "coaching-state.yaml missing" in show
    doctor = " ".join(runner.invoke(cli.app_cli, ["doctor", "--offline"]).stdout.split())
    assert "profile folder:" in doctor and "profile conflicts: possible conflict copies: events (1).yaml" in doctor
    status = json.loads(runner.invoke(cli.app_cli, ["status", "--json"]).stdout)
    assert status["profile"]["shared"] is True and status["profile"]["conflicts"] == ["events (1).yaml"]

    _offline(target)
    doctor = runner.invoke(cli.app_cli, ["doctor", "--offline"])
    assert doctor.exit_code == 1 and "is not reachable" in " ".join(doctor.stdout.split())
    assert "NOT REACHABLE" in runner.invoke(cli.app_cli, ["status"]).stdout
    assert runner.invoke(cli.app_cli, ["profile", "show"]).exit_code == 1
    assert runner.invoke(cli.app_cli, ["profile", "unlink"]).exit_code == 0 and paths.shared_profile is None


def test_cli_link_on_the_second_machine_copies_nothing(paths: Paths, drive: Path) -> None:
    write_inputs(paths)
    target = drive / "coach"
    target.mkdir()
    (target / "athlete.yaml").write_text("name: Dal primo computer\n", encoding="utf-8")
    r = runner.invoke(cli.app_cli, ["profile", "link", str(target)])  # no question: nothing would be copied
    out = " ".join(r.stdout.split())
    assert r.exit_code == 0 and "using the profile already there: athlete.yaml" in out and "no longer read" in out


# -- 6. key expiry and cloud detection --------------------------------------------------------------------------------
def _key(age: int | None) -> KeyFileStatus:
    return KeyFileStatus(Path("~/.config/garmin/connect-personal-api-key"), True, "file", None, age)


def test_key_warning_threshold() -> None:
    assert key_warning(_key(KEY_WARN_DAYS)) is None and key_warning(_key(None)) is None
    assert f"{KEY_WARN_DAYS + 1} days old" in (key_warning(_key(KEY_WARN_DAYS + 1)) or "")


def test_key_warning_reaches_the_coach(paths: Paths, settings: Settings, clock: FakeClock,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    warm(paths, settings, clock)
    svc = CoachService(make_app(paths, settings, clock, Fetchers()))
    monkeypatch.setattr(tools_mod, "key_file_status", lambda: _key(3))
    assert "key_warning" not in svc.get_events()
    monkeypatch.setattr(tools_mod, "key_file_status", lambda: _key(360))
    assert "360 days old" in svc.get_events()["key_warning"]


def test_macos_cloud_mounts_are_recognised() -> None:
    assert is_cloud_synced(Path("/Users/me/Library/CloudStorage/GoogleDrive-me@example.com/My Drive/x"))
    assert not is_cloud_synced(Path("/Users/me/Library/Application Support/garmin-coach"))
