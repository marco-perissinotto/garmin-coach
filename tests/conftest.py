from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeClock, FakeGarmin  # noqa: E402

from garmin_coach.settings import Paths, Settings  # noqa: E402
from garmin_coach.store import db  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch the owner's real app data dir."""
    monkeypatch.setenv("GARMIN_COACH_HOME", str(tmp_path / "home"))


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "home").ensure()


@pytest.fixture
def settings() -> Settings:
    s = Settings()
    s.sync.min_call_gap_ms = 0
    s.sync.lock_wait_seconds = 2
    return s


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake() -> FakeGarmin:
    return FakeGarmin()


@pytest.fixture
def conn(paths: Paths):  # type: ignore[no-untyped-def]
    c = db.connect(paths.db)
    yield c
    c.close()
