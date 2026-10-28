"""garmin-coach command line (PLAN §6.1). Phase 1 commands: doctor, backfill, sync, normalize, status, jobs, mcp."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from .app import App
from .garmin.endpoints import REFRESH_TIERS, Tier
from .secrets import setup_logging
from .status import build_status
from .sync import jobs as jobs_mod
from .sync import normalize as normalize_mod
from .sync.startup import startup_check

app_cli = typer.Typer(no_args_is_help=True, add_completion=False, help="Local Garmin cache + coaching context.")
jobs_cli = typer.Typer(no_args_is_help=True, help="Job history and control.")
app_cli.add_typer(jobs_cli, name="jobs")
console = Console()
err = Console(stderr=True)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _app(console_level: int = logging.WARNING) -> App:
    a = App.load()
    setup_logging(a.paths, console_level=console_level)
    return a


def _parse_tiers(value: str | None) -> set[Tier]:
    if not value:
        return set(REFRESH_TIERS)
    try:
        return {Tier(x.strip().upper()) for x in value.split(",") if x.strip()}
    except ValueError:
        raise typer.BadParameter("tiers are letters among A,B,C,D,E") from None


def _spawn_detached(args: list[str]) -> int:
    """--background in a CLI: the job must outlive this process, so run it in a detached child."""
    cmd = [sys.executable, "-m", "garmin_coach", *args]
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs).pid  # noqa: S603 - fixed argv, no shell


def _wait_job(a: App, job_id: int, *, quiet: bool = False) -> dict[str, Any]:
    """Foreground wait with a progress bar. Ctrl-C stops waiting; the job dies with this process and is resumed
    by the next start (orphan -> resume)."""
    conn = a.connect()
    job = jobs_mod.get_job(conn, job_id) or {}
    if quiet:
        while job.get("status") not in jobs_mod.TERMINAL:
            time.sleep(0.5)
            job = jobs_mod.get_job(conn, job_id) or {}
        return job
    with Progress(TextColumn("[bold]#{task.fields[jid]} {task.fields[kind]}"), BarColumn(), MofNCompleteColumn(),
                  TimeElapsedColumn(), TextColumn("{task.description}"), console=err, transient=False) as bar:
        task = bar.add_task("queued", total=None, jid=job_id, kind=job.get("kind", ""))
        while True:
            job = jobs_mod.get_job(conn, job_id) or {}
            planned = job.get("calls_planned") or None
            done = (job.get("calls_done") or 0) + (job.get("calls_failed") or 0)
            bar.update(task, total=planned, completed=done, description=(job.get("current_step") or job.get("status") or "")[:70])
            if job.get("status") in jobs_mod.TERMINAL:
                break
            time.sleep(0.5)
    conn.close()
    return job


def _report(job: dict[str, Any]) -> None:
    msg = (f"job #{job.get('id')} {job.get('status')}: {job.get('calls_done')}/{job.get('calls_planned')} calls ok, "
           f"{job.get('calls_failed')} failed ({job.get('notes') or ''})")
    if job.get("error"):
        msg += f" - {job['error']}"
    err.print(msg)


def _run_startup_then(a: App, *, refresh_stale: bool) -> None:
    jid = startup_check(a.jobs, a.settings, trigger="cli", refresh_stale=refresh_stale)
    if jid:
        err.print(f"startup check: enqueued job #{jid} ({jobs_mod.get_job(a.conn, jid)['kind']})")  # type: ignore[index]
        _report(_wait_job(a, jid))


# -- commands ----------------------------------------------------------------------------------------------------
@app_cli.command()
def doctor(offline: Annotated[bool, typer.Option("--offline", help="Skip the token exchange/API probe.")] = False) -> None:
    """Check key file, token exchange, database and config/knowledge files. Never prints secrets."""
    from .doctor import run_doctor

    a = _app()
    checks, nxt = run_doctor(a.paths, a.settings, repo_root=REPO_ROOT, online=not offline)
    for c in checks:
        mark = "[green]OK  [/]" if c.ok else ("[red]FAIL[/]" if c.critical else "[yellow]WARN[/]")
        console.print(f"{mark} {c.name}: {c.detail}", highlight=False)
    failed = any(not c.ok and c.critical for c in checks)
    console.print(f"\nNext: [bold]{nxt}[/]", highlight=False)
    raise typer.Exit(1 if failed else 0)


@app_cli.command()
def backfill(
    days: Annotated[int | None, typer.Option(help="History window in days (default settings.sync.backfill_days).")] = None,
    fit: Annotated[bool, typer.Option("--fit", help="Also download FIT archives (Phase 5, not yet implemented).")] = False,
    background: Annotated[bool, typer.Option("--background", help="Run detached; watch with `status --watch`.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", hidden=True)] = False,
) -> None:
    """Load history (idempotent, resumable). Exit code 0 done, 2 partial, 1 failed."""
    if fit:
        err.print("--fit is a Phase 5 feature and is ignored for now.")
    if background:
        pid = _spawn_detached(["backfill", "--quiet"] + (["--days", str(days)] if days else []))
        err.print(f"backfill started in background (pid {pid}); watch with: garmin-coach status --watch")
        return
    a = _app()
    if days:
        a.settings.sync.backfill_days = days
    jobs_mod.cleanup_orphans(a.conn, a.settings)
    active = a.jobs.has_active(kind="backfill")
    jid = active["id"] if active else a.jobs.enqueue("backfill", {Tier.H}, "cli")
    if active:
        err.print(f"backfill job #{jid} already active; following it")
    while True:  # follow the chain of budget-limited backfill jobs until the history is complete
        job = _wait_job(a, jid, quiet=quiet)
        _report(job)
        nxt = a.jobs.has_active(kind="backfill")
        if job.get("status") != "partial" or not nxt:
            break
        jid = nxt["id"]
    raise typer.Exit(jobs_mod.EXIT_CODES.get(job.get("status", "failed"), 1))


@app_cli.command()
def sync(
    tier: Annotated[str | None, typer.Option("--tier", help="Comma-separated tiers, e.g. A,B (default all A-E).")] = None,
    force: Annotated[bool, typer.Option("--force", help="Ignore TTLs and refetch the tiers now.")] = False,
    background: Annotated[bool, typer.Option("--background", help="Run detached; watch with `status --watch`.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", hidden=True)] = False,
) -> None:
    """Refresh now. Without --force the TTLs apply, so it is a no-op when everything is fresh."""
    tiers = _parse_tiers(tier)
    if background:
        args = ["sync", "--quiet"] + (["--tier", tier] if tier else []) + (["--force"] if force else [])
        pid = _spawn_detached(args)
        err.print(f"sync started in background (pid {pid}); watch with: garmin-coach status --watch")
        return
    a = _app()
    _run_startup_then(a, refresh_stale=False)
    jid = a.jobs.enqueue("refresh", tiers, "cli", force=force)
    job = _wait_job(a, jid, quiet=quiet)
    _report(job)
    raise typer.Exit(jobs_mod.EXIT_CODES.get(job.get("status", "failed"), 1))


@app_cli.command()
def normalize(all_: Annotated[bool, typer.Option("--all", help="Rebuild every normalized table from raw.")] = False) -> None:
    """Rebuild normalized tables from raw_payload. Offline: never contacts Garmin."""
    if not all_:
        raise typer.BadParameter("only `normalize --all` is supported")
    a = _app()
    t0 = time.perf_counter()
    n = normalize_mod.rebuild_all(a.conn)
    counts = {t: a.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in normalize_mod.NORMALIZED_TABLES}
    err.print(f"normalized {n} payloads in {time.perf_counter() - t0:.2f}s: " +
              ", ".join(f"{k}={v}" for k, v in counts.items()))


def _status_table(s: dict[str, Any]) -> Table:
    t = Table(title="garmin-coach cache", show_lines=False)
    for col in ("tier", "age", "ttl", "state", "note"):
        t.add_column(col)
    for name, v in s["tiers"].items():
        age = "never" if v["age_minutes"] is None else f"{v['age_minutes']} min"
        ttl = "once" if v["ttl_minutes"] is None else f"{v['ttl_minutes']} min"
        if name == "H":
            state = "[green]loaded[/]" if not v["stale"] else "[yellow]not loaded[/]"
        else:
            state = "[red]stale[/]" if v["stale"] else "[green]fresh[/]"
        note = f"cooldown until {v['cooldown_until']}" if v["cooldown_until"] else (v["last_error"] or "")
        t.add_row("H (history)" if name == "H" else name, age, ttl, state, note[:70])
    hist = f"loaded {s['history_loaded_at']}" if s["history_loaded"] else "[yellow]not loaded[/]"
    t.caption = f"history: {hist} | key age: {s['key_age_days']} d"
    j = s.get("job")
    if j:
        t.caption += (f"\njob #{j['id']} {j['kind']} {j['status']}: {j['calls_done']}/{j['calls_planned']} "
                      f"({j['calls_failed']} failed) | {j['current_step'] or ''} | ETA {j['eta_seconds']} s")
    elif s["last_jobs"]:
        lj = s["last_jobs"][0]
        t.caption += f"\nlast job #{lj['id']} {lj['kind']} {lj['status']} at {lj['finished_at']}"
    return t


@app_cli.command()
def status(
    watch: Annotated[bool, typer.Option("--watch", help="Refresh every 2 s until the running job ends.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable, same object as get_sync_status.")] = False,
) -> None:
    """Cache age per tier, history loaded?, running job with progress, cooldowns, key age. Never refreshes."""
    a = _app()
    s = build_status(a.conn, a.settings)
    if as_json:
        print(json.dumps(s, indent=2, default=str))
        return
    if not watch:
        console.print(_status_table(s))
        return
    try:
        with Live(_status_table(s), console=console, refresh_per_second=1) as live:
            seen_job = s.get("job") is not None
            while True:
                time.sleep(2)
                s = build_status(a.conn, a.settings)
                live.update(_status_table(s))
                seen_job = seen_job or s.get("job") is not None
                if seen_job and s.get("job") is None:
                    break
    except KeyboardInterrupt:
        pass


@jobs_cli.command("list")
def jobs_list(limit: int = 20) -> None:
    a = _app()
    t = Table()
    for col in ("id", "kind", "trigger", "tiers", "status", "calls", "failed", "queued", "finished", "error"):
        t.add_column(col)
    for j in jobs_mod.list_jobs(a.conn, limit):
        t.add_row(str(j["id"]), j["kind"], j["trigger"], j["tiers"], j["status"],
                  f"{j['calls_done']}/{j['calls_planned']}", str(j["calls_failed"]), j["queued_at"] or "",
                  j["finished_at"] or "", (j["error"] or "")[:40])
    console.print(t)


@jobs_cli.command("show")
def jobs_show(job_id: int) -> None:
    a = _app()
    j = jobs_mod.get_job(a.conn, job_id)
    if j is None:
        raise typer.Exit(1)
    print(json.dumps(j, indent=2))


@jobs_cli.command("cancel")
def jobs_cancel(job_id: int) -> None:
    a = _app()
    err.print(jobs_mod.request_cancel(a.conn, job_id) or "no such job")


@jobs_cli.command("prune")
def jobs_prune(keep: int = 50) -> None:
    a = _app()
    err.print(f"deleted {jobs_mod.prune_jobs(a.conn, keep)} job rows")


@app_cli.command()
def mcp() -> None:
    """Start the MCP server on stdio (runs the startup check first; logs go to stderr/file, never stdout)."""
    from .mcp.server import serve

    serve(_app())


def main() -> None:
    app_cli()


if __name__ == "__main__":
    main()
