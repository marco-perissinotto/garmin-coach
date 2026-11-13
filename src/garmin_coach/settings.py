"""Paths (platformdirs), settings.yaml loading and environment overrides (PLAN §5.1, §11.1)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_data_dir
from pydantic import BaseModel, ConfigDict, Field

APP_NAME = "garmin-coach"
HOME_ENV_VAR = "GARMIN_COACH_HOME"

CONFIG_FILES = ("athlete.yaml", "events.yaml")
KNOWLEDGE_FILES = ("history.md", "coach-notes.md")
STATE_FILE = "coaching-state.yaml"  # optional short-term coaching state (PLAN §11.3), not a required input
PROFILE_FILES = (*CONFIG_FILES, *KNOWLEDGE_FILES, STATE_FILE, "settings.yaml")  # what `profile link` shares (D-085)
PROFILE_POINTER = "profile-path"  # one line in the app data dir: the folder that replaces <home>/config


class ProfileUnavailable(OSError):
    """A write was asked while the linked profile folder is not there (sync client not running, folder offline)."""


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def local_config_dir(self) -> Path:
        return self.home / "config"

    @property
    def profile_pointer(self) -> Path:
        return self.home / PROFILE_POINTER

    @property
    def shared_profile(self) -> Path | None:
        """The folder set by `garmin-coach profile link`, or None when the personal files are local (D-085)."""
        try:
            text = self.profile_pointer.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return Path(text) if text else None

    @property
    def config_dir(self) -> Path:
        """Where the personal files are read from: the linked folder if there is one, else ``<home>/config``."""
        return self.shared_profile or self.local_config_dir

    def writable_config_dir(self) -> Path:
        """The folder for a write. A linked folder that is missing is never created: where the sync client is not
        running, that would start a second, empty profile next to the real one (D-087)."""
        shared = self.shared_profile
        if shared is None:
            self.local_config_dir.mkdir(parents=True, exist_ok=True)
            return self.local_config_dir
        if not shared.is_dir():
            raise ProfileUnavailable(f"the shared profile folder {shared} is not reachable: start the sync client "
                                     "(and keep the folder available offline), then retry")
        return shared

    @property
    def db(self) -> Path:
        return self.home / "garmin.db"

    @property
    def lock(self) -> Path:
        return self.home / "refresh.lock"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "garmin-coach.log"

    @property
    def context_dir(self) -> Path:
        return self.home / "context"

    @property
    def fit_dir(self) -> Path:
        return self.home / "fit"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.yaml"

    def ensure(self) -> Paths:
        dirs = [self.home, self.logs_dir]
        if self.shared_profile is None:
            dirs.append(self.local_config_dir)
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        return self


def app_home() -> Path:
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser().resolve()
    # Windows: %LOCALAPPDATA%\garmin-coach (not roaming, not synced); macOS: ~/Library/Application Support/garmin-coach
    return Path(user_data_dir(APP_NAME, appauthor=False, roaming=False))


def get_paths() -> Paths:
    return Paths(app_home())


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class CooldownMinutes(_Model):
    rate_limit: int = 120
    network_error: int = 10


class SyncSettings(_Model):
    ttl_minutes: dict[str, int] = Field(
        default_factory=lambda: {"A": 60, "B": 60, "C": 1440, "D": 1440, "E": 10080}
    )
    min_call_gap_ms: int = 500
    backfill_call_gap_ms: int = 1000  # slower pacing for the long history load (429s seen after ~195 calls/2 min)
    sync_budget_calls: int = 30
    sync_timeout_seconds: float = 25
    background_budget_calls: int = 60
    backfill_budget_calls: int = 400
    backfill_days: int = 365
    detail_days: int = 28
    startup_check: bool = True
    lock_wait_seconds: float = 15
    heartbeat_stale_seconds: int = 120
    cooldown_minutes: CooldownMinutes = Field(default_factory=CooldownMinutes)


class FitSettings(_Model):
    """FIT download and parsing (docs/FIT.md). Replaces the old ``sync.download_fit`` (D-076)."""

    download: bool = True  # download + parse the FIT of every new activity during sync (tier A, history)
    keep_files: bool = True  # keep <app data>/fit/<activityId>.zip for offline re-parsing
    skip_types: list[str] = Field(default_factory=list)  # Garmin typeKeys never downloaded, e.g. [walking, hiking]
    backfill_days: int = 0  # `fit backfill` window in days; 0 = the whole activity history in the cache
    backfill_budget_calls: int = 100  # per fit job; jobs chain until every eligible activity has been tried
    call_gap_ms: int = 1500  # pacing of fit jobs (ZIP responses are heavier than JSON)
    max_laps: int = 300  # laps kept per activity (meta.laps_truncated when cut)
    series_points: int = 48  # points of the coarse intensity profile


class OptionalEndpoints(_Model):
    nutrition: bool = False
    womens_health: bool = False
    hydration: bool = True
    spo2: bool = True


class ContextSettings(_Model):
    daily_token_budget: int = 3500  # includes the coaching state (~1 000 tokens at most, D-062)
    weekly_token_budget: int = 6000
    recent_activities: int = 10
    state_stale_days: int = 7  # a coaching state older than this is flagged and must be confirmed (D-063)


class Settings(_Model):
    sync: SyncSettings = Field(default_factory=SyncSettings)
    fit: FitSettings = Field(default_factory=FitSettings)
    optional_endpoints: OptionalEndpoints = Field(default_factory=OptionalEndpoints)
    context: ContextSettings = Field(default_factory=ContextSettings)

    def ttl_minutes(self, tier: str) -> int:
        return int(self.sync.ttl_minutes.get(tier, 60))


def load_settings(paths: Paths | None = None) -> Settings:
    paths = paths or get_paths()
    data: dict[str, Any] = {}
    if paths.settings_file.exists():
        loaded = yaml.safe_load(paths.settings_file.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = loaded
    settings = Settings.model_validate(data)
    # Partial ttl overrides keep the defaults for the other tiers.
    settings.sync.ttl_minutes = {**SyncSettings().ttl_minutes, **settings.sync.ttl_minutes}
    return settings


def is_cloud_synced(path: Path) -> bool:
    """Best-effort check that a path is not inside OneDrive/iCloud/Dropbox (PLAN §1.2, §5.1)."""
    resolved = str(path.expanduser().resolve()).lower()
    roots = [os.environ.get(v) for v in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")]
    for root in filter(None, roots):
        if resolved.startswith(str(Path(root).resolve()).lower()):
            return True
    # "cloudstorage" / "googledrive": macOS File Provider mounts (~/Library/CloudStorage/GoogleDrive-<account>/...)
    markers = ("onedrive", "icloud", "mobile documents", "dropbox", "google drive", "googledrive", "cloudstorage")
    return any(m in resolved for m in markers)
