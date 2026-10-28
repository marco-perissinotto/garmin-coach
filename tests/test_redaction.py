from __future__ import annotations

import io
import logging
import re
import subprocess
from pathlib import Path

from garmin_coach.secrets import MASK, RedactingFilter, RedactingFormatter, redact

FAKE_KEY = "GCDC" + "_" + "abcDEF123456_fake-key-for-tests"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiJ0ZXN0In0" + ".c2lnbmF0dXJl"
KEY_SHAPED = re.compile("GCDC" + r"_[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")
REPO = Path(__file__).resolve().parents[1]


def test_redact_patterns() -> None:
    s = redact(f"key={FAKE_KEY} auth=Bearer {FAKE_JWT} raw={FAKE_JWT} hdr=ApiKey {FAKE_KEY}")
    assert FAKE_KEY not in s and FAKE_JWT not in s
    assert "Bearer " + MASK in s and "ApiKey " + MASK in s


def test_logger_output_is_masked() -> None:
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(RedactingFormatter("%(message)s"))
    h.addFilter(RedactingFilter())
    log = logging.getLogger("test.redaction")
    log.addHandler(h)
    log.propagate = False
    try:
        log.warning("token %s and key %s", FAKE_JWT, FAKE_KEY)
        try:
            raise RuntimeError(f"boom {FAKE_KEY}")
        except RuntimeError:
            log.exception("failed with Bearer %s", FAKE_JWT)
    finally:
        log.removeHandler(h)
    out = buf.getvalue()
    assert FAKE_KEY not in out and FAKE_JWT not in out
    assert MASK in out


def test_repo_contains_no_key_or_jwt() -> None:
    """Secret scan over every file git would commit (tracked + untracked-not-ignored)."""
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()
    offenders = []
    for f in files:
        p = REPO / f
        if not p.is_file() or p.suffix in {".png", ".zip", ".fit"}:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if KEY_SHAPED.search(text):
            offenders.append(f)
    assert offenders == []
