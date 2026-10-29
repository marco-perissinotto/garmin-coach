"""API key resolution (official ``resolve_api_key``) and redaction helpers (PLAN §0.1, §6.3)."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .settings import Paths

_KEY_PREFIX = "GCDC" + "_"  # split so the literal never appears in the repo
_PATTERNS = [
    re.compile(re.escape(_KEY_PREFIX) + r"[A-Za-z0-9_\-+/=.]*"),
    re.compile(r"(?i)\b(Bearer|ApiKey)\s+[A-Za-z0-9_\-+/=.]+"),
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"),
]
MASK = "***REDACTED***"


def redact(text: object) -> str:
    s = str(text)
    for p in _PATTERNS:
        s = p.sub(lambda m: (m.group(1) + " " + MASK) if m.lastindex else MASK, s)
    return s


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class RedactingFilter(logging.Filter):
    """Masks the message before any handler sees it (defence in depth with RedactingFormatter)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash the app
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        return True


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_configured = False


def setup_logging(paths: Paths, *, console_level: int = logging.WARNING) -> None:
    """File log (rotating, redacted) + stderr. Never stdout: the MCP stdio transport owns it."""
    global _configured
    root = logging.getLogger()
    if _configured:
        return
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        paths.log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(console_level)
    for h in (fh, sh):
        h.setFormatter(RedactingFormatter(_LOG_FORMAT))
        h.addFilter(RedactingFilter())
        root.addHandler(h)
    root.setLevel(logging.INFO)
    # urllib3 debug lines could include headers in some configurations.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _configured = True


# Same values as garmin_personal_api.constants; importing that package pulls in requests, which the offline
# commands (context show --offline, status) do not need.
DEFAULT_API_KEY_FILE = "~/.config/garmin/connect-personal-api-key"
DEFAULT_API_KEY_ENV_VAR = "GARMIN_PERSONAL_API_KEY"


def key_file_path() -> Path:
    return Path(DEFAULT_API_KEY_FILE).expanduser()


def load_api_key() -> str:
    """Env var GARMIN_PERSONAL_API_KEY, else the official default key file. Raises GarminPersonalApiError."""
    from garmin_personal_api import resolve_api_key

    return resolve_api_key()


@dataclass
class KeyFileStatus:
    path: Path
    exists: bool
    source: str  # "env" | "file" | "missing"
    permissions_ok: bool | None  # None = not checkable on this OS
    age_days: int | None


def key_file_status() -> KeyFileStatus:
    path = key_file_path()
    if os.environ.get(DEFAULT_API_KEY_ENV_VAR):
        return KeyFileStatus(path, path.exists(), "env", None, None)
    if not path.exists():
        return KeyFileStatus(path, False, "missing", None, None)
    st = path.stat()
    perms_ok: bool | None = None
    if os.name == "posix":
        perms_ok = not (st.st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    age = (datetime.now(UTC) - datetime.fromtimestamp(st.st_mtime, UTC)).days
    return KeyFileStatus(path, True, "file", perms_ok, age)
