"""Offline FIT commands: `fit show`, `fit reparse`, `status --json`, `doctor --offline` (docs/FIT.md).

`fit backfill` is only exercised up to its "disabled" exit: past that point it would contact Garmin.
"""

from __future__ import annotations

import json

import fitgen
import pytest
from fakes import FakeClock
from typer.testing import CliRunner

from garmin_coach import cli
from garmin_coach.fit import store as fit_store
from garmin_coach.settings import Paths, Settings
from garmin_coach.store import db

runner = CliRunner()


@pytest.fixture
def home(paths: Paths, clock: FakeClock) -> Paths:
    conn = db.connect(paths.db)
    conn.execute("INSERT INTO activity(activity_id, start_local, type_key) VALUES (1, '2026-10-27 07:00:00', "
                 "'road_biking'), (2, '2026-10-26 07:00:00', 'running')")
    fit_store.ingest(conn, paths, Settings(), 1, fitgen.activity_zip(1), clock())
    conn.close()
    return paths


def test_fit_show(home: Paths) -> None:
    r = runner.invoke(cli.app_cli, ["fit", "show", "1", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["session"]["sport"] == "cycling"
    table = runner.invoke(cli.app_cli, ["fit", "show", "1"])
    assert table.exit_code == 0 and "intervals" in table.stdout
    missing = runner.invoke(cli.app_cli, ["fit", "show", "2"])
    assert missing.exit_code == 1 and "no FIT downloaded" in missing.output


def test_fit_reparse(home: Paths) -> None:
    r = runner.invoke(cli.app_cli, ["fit", "reparse"])
    assert r.exit_code == 0 and "nothing to do" in r.output
    conn = db.connect(home.db)
    conn.execute("UPDATE activity_fit SET parser_version=0")
    conn.close()
    r = runner.invoke(cli.app_cli, ["fit", "reparse"])
    assert "re-parsed 1 files" in r.output and "ok=1" in r.output
    (home.fit_dir / "1.zip").unlink()
    r = runner.invoke(cli.app_cli, ["fit", "reparse", "--all"])
    assert "no file kept for 1 activities (1)" in " ".join(r.output.split())


def test_status_json_has_the_fit_block(home: Paths) -> None:
    r = runner.invoke(cli.app_cli, ["status", "--json"])
    fit = json.loads(r.stdout)["fit"]
    assert fit["ok"] == 1 and fit["pending"] == 2 and fit["enabled"] is True  # 1 never tried by sync_state
    text = runner.invoke(cli.app_cli, ["status"])
    assert "FIT: 1 ok" in text.stdout


def test_doctor_offline_fit_checks(home: Paths) -> None:
    home.settings_file.write_text("sync:\n  download_fit: true\n", encoding="utf-8")
    r = runner.invoke(cli.app_cli, ["doctor", "--offline"])
    out = " ".join(r.stdout.split())
    assert "FIT SDK: garmin-fit-sdk 21.218.0" in out
    assert "FIT folder:" in out and "FIT coverage: 1 ok" in out
    assert "sync.download_fit is obsolete" in out


def test_fit_backfill_refuses_when_disabled(home: Paths) -> None:
    home.settings_file.write_text("fit:\n  download: false\n", encoding="utf-8")
    r = runner.invoke(cli.app_cli, ["fit", "backfill"])
    assert r.exit_code == 1 and "fit.download is false" in r.output
