"""`garmin-coach doctor`: environment checks without ever printing secrets (PLAN §6.1, Phase 0.5)."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from .clock import Clock, local_today, system_clock
from .garmin import endpoints as ep
from .garmin.client import GarminClient, GarminError
from .secrets import key_file_status, redact
from .settings import CONFIG_FILES, KNOWLEDGE_FILES, STATE_FILE, Paths, Settings, is_cloud_synced
from .state import derived, load_state
from .store import db, repo

KNOWLEDGE_TOKEN_WARN = 3000


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    critical: bool = True


def _approx_tokens(text: str) -> int:
    return len(text) // 4


def run_doctor(paths: Paths, settings: Settings, *, repo_root: Path, online: bool = True,
               client_factory: Callable[[], GarminClient] | None = None,
               clock: Clock = system_clock) -> tuple[list[Check], str]:
    checks: list[Check] = []

    checks.append(Check("repo location", not is_cloud_synced(repo_root),
                        f"{repo_root}" + (" is inside a cloud-synced folder!" if is_cloud_synced(repo_root) else "")))
    checks.append(Check("app data location", not is_cloud_synced(paths.home),
                        f"{paths.home}" + (" is inside a cloud-synced folder!" if is_cloud_synced(paths.home) else "")))

    key = key_file_status()
    if key.source == "env":
        checks.append(Check("API key", True, "from environment variable GARMIN_PERSONAL_API_KEY"))
    elif not key.exists:
        checks.append(Check("API key", False, f"key file not found at {key.path}"))
    else:
        detail = f"{key.path} (age {key.age_days} days)"
        if key.permissions_ok is False:
            checks.append(Check("API key", False, detail + " is readable by others: run chmod 600"))
        else:
            checks.append(Check("API key", True, detail + ("" if key.permissions_ok else "; permissions not checked on Windows")))
        if is_cloud_synced(key.path):
            checks.append(Check("API key location", False, "key file is inside a cloud-synced folder"))
        if key.age_days is not None and key.age_days > 350:
            checks.append(Check("API key age", False, "key is older than 350 days: rotate it soon", critical=False))

    if online and key.source != "missing":
        client = client_factory() if client_factory else GarminClient(budget=2, min_gap_s=0)
        try:
            body = client.fetch(ep.get("profile.settings"), {})
            units = body.get("measurementSystem") if isinstance(body, dict) else None
            tz = body.get("timeZone") if isinstance(body, dict) else None
            checks.append(Check("token exchange + profile settings", True,
                                f"OK (measurementSystem={units}, timeZone={tz})"))
        except GarminError as exc:
            checks.append(Check("token exchange + profile settings", False, redact(str(exc))))
        finally:
            client.close()
    elif not online:
        checks.append(Check("token exchange + profile settings", True, "skipped (--offline)", critical=False))

    cache_empty = True
    try:
        conn = db.connect(paths.db)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        cache_empty = repo.is_cache_empty(conn)
        n_raw = conn.execute("SELECT COUNT(*) FROM raw_payload").fetchone()[0]
        conn.close()
        checks.append(Check("database", True,
                            f"{paths.db} (schema v{version}, {mode}, {n_raw} raw payloads, "
                            f"history {'NOT loaded yet' if cache_empty else 'loaded'})"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("database", False, f"{paths.db}: {redact(str(exc))}"))

    for name in CONFIG_FILES:
        p = paths.config_dir / name
        if not p.exists():
            checks.append(Check(name, False, f"missing: copy config/{name.replace('.yaml', '.example.yaml')} to {p}"))
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            ok = isinstance(data, dict)
            checks.append(Check(name, ok, str(p) if ok else f"{p} is not a YAML mapping"))
        except yaml.YAMLError as exc:
            checks.append(Check(name, False, f"{p}: invalid YAML ({exc.__class__.__name__})"))
    for name in KNOWLEDGE_FILES:
        p = paths.config_dir / name
        if not p.exists():
            checks.append(Check(name, False, f"missing: copy config/{name.replace('.md', '.example.md')} to {p}"))
            continue
        tokens = _approx_tokens(p.read_text(encoding="utf-8"))
        if tokens > KNOWLEDGE_TOKEN_WARN:
            checks.append(Check(name, True, f"{p} (~{tokens} tokens: above {KNOWLEDGE_TOKEN_WARN}, consider trimming)",
                                critical=False))
        else:
            checks.append(Check(name, True, f"{p} (~{tokens} tokens)"))

    checks.append(_state_check(paths, settings, local_today(clock())))
    checks.extend(_fit_checks(paths, settings, local_today(clock())))

    failed = [c for c in checks if not c.ok and c.critical]
    if any(c.name == "API key" for c in failed):
        nxt = "create the key file, then run: garmin-coach doctor"
    elif failed:
        nxt = "fix the failed checks above, then run: garmin-coach doctor"
    else:
        from .install import next_step_for_clients

        client_step = next_step_for_clients(repo_root)
        if cache_empty and client_step:
            nxt = client_step + "   (the history loads by itself at the first server start)"
        elif cache_empty:
            nxt = "garmin-coach backfill   (or just start `garmin-coach mcp`: the history loads by itself)"
        else:
            nxt = client_step or "garmin-coach status"
    return checks, nxt


def _fit_checks(paths: Paths, settings: Settings, today: date) -> list[Check]:
    """FIT ingestion (docs/FIT.md): the SDK, the folder for the kept files, the obsolete setting, the coverage."""
    from importlib.metadata import PackageNotFoundError, version

    from .fit import store as fit_store

    out: list[Check] = []
    enabled = settings.fit.download
    try:
        import garmin_fit_sdk  # noqa: F401 - only checks that it imports

        out.append(Check("FIT SDK", True, f"garmin-fit-sdk {version('garmin-fit-sdk')}"
                         + ("" if enabled else " (FIT downloads disabled in settings)")))
    except (ImportError, PackageNotFoundError):
        out.append(Check("FIT SDK", False, "garmin-fit-sdk is not installed: run `uv sync`", critical=enabled))
    if settings.fit.keep_files:
        try:
            paths.fit_dir.mkdir(parents=True, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=".doctor.", dir=paths.fit_dir)
            os.close(fd)
            os.unlink(probe)
            out.append(Check("FIT folder", True, f"{paths.fit_dir} ({fit_store.files_mb(paths)} MB)"))
        except OSError as exc:
            out.append(Check("FIT folder", False, f"{paths.fit_dir} is not writable ({type(exc).__name__}): FIT "
                             "summaries are still stored, the files are not kept", critical=False))
    if paths.settings_file.exists():
        try:
            raw = yaml.safe_load(paths.settings_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            raw = {}
        sync = raw.get("sync") if isinstance(raw, dict) else None
        if isinstance(sync, dict) and "download_fit" in sync:
            out.append(Check("settings.yaml", False, "sync.download_fit is obsolete and ignored: use the `fit:` block "
                             "(fit.download, see config/settings.example.yaml)", critical=False))
    try:
        conn = db.connect(paths.db)
        try:
            cov = fit_store.coverage(conn, settings, paths, today)
        finally:
            conn.close()
        out.append(Check("FIT coverage", True, f"{cov['ok']} ok, {cov['pending']} pending, {cov['no_file']} no file, "
                         f"{cov['parse_error']} parse errors" + ("" if not cov["pending"] or not enabled else
                                                                 " (load the past with: garmin-coach fit backfill)"),
                         critical=False))
    except Exception as exc:  # noqa: BLE001 - the database check above already reports a broken DB
        out.append(Check("FIT coverage", False, redact(str(exc)), critical=False))
    return out


def _state_check(paths: Paths, settings: Settings, today: date) -> Check:
    """The optional coaching state: absent is fine, otherwise its age and the same size warning as the knowledge."""
    p = paths.config_dir / STATE_FILE
    if not p.exists():
        return Check(STATE_FILE, True, "none yet (the coach writes it with update_coaching_state)", critical=False)
    state, problems = load_state(paths, today)
    if state is None:
        detail = "; ".join(problems) or "empty file"
        return Check(STATE_FILE, not problems, f"{p}: {detail}", critical=False)
    d = derived(state, today, settings.context.state_stale_days)
    tokens = _approx_tokens(p.read_text(encoding="utf-8"))
    detail = f"{p} (updated {d['age_days']} days ago by {d['updated_by']}, ~{tokens} tokens"
    if d["stale"]:
        detail += f"; older than {d['stale_after_days']} days: the coach will ask to confirm it"
    if tokens > KNOWLEDGE_TOKEN_WARN:
        detail += f"; above {KNOWLEDGE_TOKEN_WARN}, consider trimming"
    return Check(STATE_FILE, True, detail + ")", critical=False)
