"""garmin-coach command line (PLAN §6.1, §9.1): doctor, backfill, sync, normalize, status, jobs, context, mcp, install."""

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

from .app import App
from .garmin.endpoints import REFRESH_TIERS, Tier
from .secrets import setup_logging


class _LazyModule:
    """Import the sync engine on first use: offline commands (context show --offline) never need it."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        import importlib

        return getattr(importlib.import_module(self._name), attr)


jobs_mod: Any = _LazyModule("garmin_coach.sync.jobs")
normalize_mod: Any = _LazyModule("garmin_coach.sync.normalize")


def startup_check(*args: Any, **kwargs: Any) -> Any:
    from .sync.startup import startup_check as check

    return check(*args, **kwargs)


def build_status(*args: Any, **kwargs: Any) -> Any:
    from .status import build_status as build

    return build(*args, **kwargs)

app_cli = typer.Typer(no_args_is_help=True, add_completion=False, help="Local Garmin cache + coaching context.")
jobs_cli = typer.Typer(no_args_is_help=True, help="Job history and control.")
app_cli.add_typer(jobs_cli, name="jobs")
context_cli = typer.Typer(no_args_is_help=True, help="Context packs for the coach (Markdown + JSON).")
app_cli.add_typer(context_cli, name="context")
install_cli = typer.Typer(no_args_is_help=True, help="Register the MCP server in Claude Desktop / Claude Code.")
app_cli.add_typer(install_cli, name="install")


class _LazyConsole:
    """rich is imported on first use only: `context show --offline` must start fast (PLAN Phase 2 acceptance)."""

    def __init__(self, stderr: bool) -> None:
        self._stderr = stderr
        self._console: Any = None

    @property
    def real(self) -> Any:
        if self._console is None:
            from rich.console import Console

            self._console = Console(stderr=self._stderr)
        return self._console

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


console = _LazyConsole(stderr=False)
err = _LazyConsole(stderr=True)
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
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

    with Progress(TextColumn("[bold]#{task.fields[jid]} {task.fields[kind]}"), BarColumn(), MofNCompleteColumn(),
                  TimeElapsedColumn(), TextColumn("{task.description}"), console=err.real, transient=False) as bar:
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
    checks, nxt = run_doctor(a.paths, a.settings, repo_root=REPO_ROOT, online=not offline, clock=a.clock)
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


def _status_table(s: dict[str, Any]) -> Any:
    from rich.table import Table

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
        from rich.live import Live

        with Live(_status_table(s), console=console.real, refresh_per_second=1) as live:
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
    from rich.table import Table

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


# -- context packs (PLAN §8) --------------------------------------------------------------------------------------
# In the CLI every tier a pack needs (render.PACK_TIERS) is refreshed synchronously.
def _fresh(a: App, tiers: set[Tier], max_age: int | None) -> Any:
    """Startup check + synchronous freshness gate. Returns the Freshness object (or None when nothing to check)."""
    if not tiers:
        return None
    _run_startup_then(a, refresh_stale=False)
    from datetime import timedelta

    ma = None if max_age is None else {t: timedelta(minutes=max_age) for t in tiers}
    return a.gate(background=False).ensure_fresh(tiers, ma, trigger="cli")


def _utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252


@context_cli.command("show")
def context_show(
    what: Annotated[str, typer.Argument(help="daily | weekly | 7d | 28d | events | athlete | recent | instructions")] = "daily",
    offline: Annotated[bool, typer.Option("--offline", help="Never contact Garmin: render from the cache.")] = False,
    max_age: Annotated[int | None, typer.Option("--max-age", help="Override the TTL in minutes (0 = force).")] = None,
) -> None:
    """Print a pack, ready to paste into a chat. Refreshes the tiers it needs first unless --offline."""
    from .context import render

    if what not in render.ALIASES:
        raise typer.BadParameter(f"choose one of: {', '.join(render.ALIASES)}")
    a = _app()
    fr = None if offline else _fresh(a, set(render.PACK_TIERS[what]), max_age)
    _, texts = render.build_and_render(a.conn, a.paths, a.settings, clock=a.clock, freshness=fr,
                                       names=render.ALIASES[what], write=False)
    _utf8_stdout()
    print("\n".join(texts.values()), end="")


@context_cli.command("build")
def context_build(
    offline: Annotated[bool, typer.Option("--offline", help="Never contact Garmin: build from the cache.")] = False,
) -> None:
    """Recompute metrics and write every pack + context.json to <app data>/context/."""
    from .context import render

    a = _app()
    fr = None if offline else _fresh(a, set(REFRESH_TIERS), None)
    _, texts = render.build_and_render(a.conn, a.paths, a.settings, clock=a.clock, freshness=fr)
    for name, tokens in render.token_report(texts).items():
        err.print(f"{name}.md  ~{tokens} token")
    err.print(f"written to {a.paths.context_dir}")


@context_cli.command("export")
def context_export(
    zip_: Annotated[bool, typer.Option("--zip", help="Bundle the packs into a zip (e.g. for a ChatGPT Project).")] = True,
    offline: Annotated[bool, typer.Option("--offline", help="Never contact Garmin: export from the cache.")] = False,
) -> None:
    """Refresh everything (unless --offline), rebuild the packs and bundle them."""
    from .context import render

    a = _app()
    fr = None if offline else _fresh(a, set(REFRESH_TIERS), None)
    render.build_and_render(a.conn, a.paths, a.settings, clock=a.clock, freshness=fr)
    target = render.export_zip(a.paths, a.clock) if zip_ else a.paths.context_dir
    err.print(f"exported: {target}")


@app_cli.command()
def mcp(
    http: Annotated[bool, typer.Option("--http", help="Streamable HTTP on 127.0.0.1 with a random bearer token.")] = False,
    port: Annotated[int, typer.Option(help="Port for --http (always bound to 127.0.0.1).")] = 8765,
) -> None:
    """Start the MCP server on stdio (runs the startup check first; logs go to stderr/file, never stdout)."""
    from .mcp.server import serve

    serve(_app(), http=http, port=port)


# -- client registration (PLAN §9.1) ------------------------------------------------------------------------------
@install_cli.command("claude-desktop")
def install_desktop(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the resulting config instead of writing it.")] = False,
) -> None:
    """Add garmin-coach to claude_desktop_config.json (absolute paths, other servers kept, backup written)."""
    from .install import InstallError, install_claude_desktop

    try:
        res = install_claude_desktop(REPO_ROOT, dry_run=dry_run)
    except InstallError as exc:
        err.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    if dry_run:
        # Only our entry: other servers' entries may carry their own secrets in `env`.
        _utf8_stdout()
        servers = res.config["mcpServers"]
        print(json.dumps({"mcpServers": {"garmin-coach": servers["garmin-coach"]}}, indent=2))
        others = sorted(k for k in servers if k != "garmin-coach")
        err.print(f"(dry run) would write {res.path}" + (f", keeping: {', '.join(others)}" if others else ""))
        return
    if not res.changed:
        err.print(f"already registered in {res.path}")
    else:
        err.print(f"registered in {res.path}" + (f" (backup: {res.backup.name})" if res.backup else ""))
    err.print("Next: quit Claude Desktop completely (also from the tray) and start it again, then ask "
              "\"How should I train today?\"")


@install_cli.command("claude-code")
def install_code(
    scope: Annotated[str, typer.Option(help="user (every folder) | local | project")] = "user",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the `claude mcp add` command only.")] = False,
) -> None:
    """Run `claude mcp add` for garmin-coach. Inside this repo Claude Code already uses the committed .mcp.json."""
    from .install import InstallError, install_claude_code

    try:
        cmd = install_claude_code(REPO_ROOT, scope, dry_run=dry_run)
    except InstallError as exc:
        err.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    print(" ".join(cmd) if dry_run else f"registered with scope '{scope}': {' '.join(cmd[:7])} ...")


def main() -> None:
    app_cli()


if __name__ == "__main__":
    main()
