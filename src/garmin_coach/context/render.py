"""Render the ContextPack into Markdown packs + context.json (PLAN §8)."""

from __future__ import annotations

import logging
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

from ..athlete import load_inputs
from ..clock import Clock, local_today, system_clock
from ..settings import Paths, Settings
from . import builder, fmt

log = logging.getLogger(__name__)

PACKS = ("daily-brief", "athlete", "events", "last-7d", "last-28d", "activities-recent", "coach-instructions")
ALIASES: dict[str, tuple[str, ...]] = {
    "daily": ("daily-brief",),
    "weekly": ("last-7d", "last-28d"),
    "7d": ("last-7d",),
    "28d": ("last-28d",),
    "events": ("events",),
    "athlete": ("athlete",),
    "recent": ("activities-recent",),
    "instructions": ("coach-instructions",),
}

_env: Environment | None = None


def env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(loader=PackageLoader("garmin_coach.context", "templates"), trim_blocks=True,
                           lstrip_blocks=True, keep_trailing_newline=True, undefined=StrictUndefined,
                           autoescape=False)  # noqa: S701 - Markdown for an LLM, not HTML
    return _env


def render(pack: builder.ContextPack, name: str) -> str:
    text = env().get_template(f"{name}.md.j2").render(p=pack)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def build_and_render(conn: sqlite3.Connection, paths: Paths, settings: Settings, *, clock: Clock = system_clock,
                     freshness: Any | None = None, names: tuple[str, ...] = PACKS,
                     write: bool = True) -> tuple[builder.ContextPack, dict[str, str]]:
    """Recompute metrics if the data changed, build the pack, render ``names`` (and write all packs if ``write``)."""
    inputs = load_inputs(paths)
    today = local_today(clock())
    builder.ensure_metrics(conn, inputs, today, builder.inputs_mtime(paths.config_dir))
    pack = builder.build(conn, inputs, clock=clock, freshness=freshness,
                         recent_n=settings.context.recent_activities)
    texts = {n: render(pack, n) for n in (PACKS if write else names)}
    if write:
        out = paths.context_dir
        out.mkdir(parents=True, exist_ok=True)
        for n, t in texts.items():
            (out / f"{n}.md").write_text(t, encoding="utf-8")
        (out / "context.json").write_text(pack.model_dump_json(indent=2), encoding="utf-8")
    return pack, {n: texts[n] for n in names}


def after_sync(conn: sqlite3.Connection, paths: Paths, settings: Settings, clock: Clock = system_clock) -> None:
    """Post-refresh hook: metrics + packs. Never raises: a rendering problem must not fail a sync."""
    try:
        build_and_render(conn, paths, settings, clock=clock)
    except Exception:  # noqa: BLE001
        log.exception("context build after sync failed")


def export_zip(paths: Paths, clock: Clock = system_clock) -> Path:
    out = paths.context_dir
    target = out / f"garmin-coach-context-{local_today(clock()).isoformat()}.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for n in PACKS:
            p = out / f"{n}.md"
            if p.exists():
                z.write(p, p.name)
        if (out / "context.json").exists():
            z.write(out / "context.json", "context.json")
    return target


def token_report(texts: dict[str, str]) -> dict[str, int]:
    return {n: fmt.approx_tokens(t) for n, t in texts.items()}
