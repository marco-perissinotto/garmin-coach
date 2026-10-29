"""`garmin-coach install claude-desktop|claude-code`: register the MCP server with absolute paths (PLAN §9.1).

Rules: absolute paths only, never secrets in ``args``/``env``, keep every other server entry of the client config,
back up the file before changing it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SERVER_NAME = "garmin-coach"
SCOPES = ("user", "local", "project")


class InstallError(Exception):
    pass


def uv_path() -> str:
    uv = shutil.which("uv")
    if not uv:
        raise InstallError("`uv` not found on PATH: install it first (https://docs.astral.sh/uv/)")
    # Absolute but not resolved: a package-manager shim (WinGet Links, Homebrew bin) survives uv upgrades.
    return str(Path(uv).absolute())


def server_args(repo_root: Path) -> list[str]:
    return ["run", "--project", str(repo_root.resolve()), "garmin-coach", "mcp"]


def desktop_config_path() -> Path:
    """Claude Desktop's config file. The Microsoft Store (MSIX) build keeps it in its package's LocalCache."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            for pkg in sorted((Path(local) / "Packages").glob("Claude_*")):
                return pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / \
            "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallError(f"{path} is not valid JSON ({exc.msg} at line {exc.lineno}): fix it by hand first") from None
    if not isinstance(data, dict):
        raise InstallError(f"{path} does not contain a JSON object")
    return data


def desktop_entry(repo_root: Path, uv: str | None = None) -> dict[str, Any]:
    return {"command": uv or uv_path(), "args": server_args(repo_root)}


def desktop_registered(path: Path | None = None) -> bool | None:
    """True/False when Claude Desktop's config exists, None when Claude Desktop does not seem installed."""
    path = path or desktop_config_path()
    if not path.parent.exists():
        return None
    try:
        return SERVER_NAME in (_read_config(path).get("mcpServers") or {})
    except InstallError:
        return False


@dataclass
class DesktopResult:
    path: Path
    config: dict[str, Any]
    changed: bool
    backup: Path | None = None


def install_claude_desktop(repo_root: Path, *, dry_run: bool = False, path: Path | None = None,
                           uv: str | None = None) -> DesktopResult:
    path = path or desktop_config_path()
    config = _read_config(path)
    servers = config.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise InstallError(f"{path}: 'mcpServers' is not an object")
    entry = desktop_entry(repo_root, uv)
    new = {**config, "mcpServers": {**(servers or {}), SERVER_NAME: entry}}
    changed = new != config
    if dry_run or not changed:
        return DesktopResult(path, new, changed)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return DesktopResult(path, new, changed, backup)


def claude_cli() -> str | None:
    return shutil.which("claude")


def claude_code_command(repo_root: Path, scope: str, *, claude: str = "claude", uv: str | None = None) -> list[str]:
    if scope not in SCOPES:
        raise InstallError(f"scope must be one of {', '.join(SCOPES)}")
    return [claude, "mcp", "add", "--scope", scope, SERVER_NAME, "--", uv or uv_path(), *server_args(repo_root)]


Run = Callable[..., subprocess.CompletedProcess[str]]


def install_claude_code(repo_root: Path, scope: str = "user", *, dry_run: bool = False,
                        run: Run = subprocess.run) -> list[str]:
    """Run ``claude mcp add`` (replacing an existing garmin-coach entry in that scope). Returns the command."""
    claude = claude_cli()
    cmd = claude_code_command(repo_root, scope, claude=claude or "claude")
    if dry_run:
        return cmd
    if not claude:
        raise InstallError("the `claude` CLI is not on PATH; run this command yourself:\n  " + " ".join(cmd))
    # `claude mcp add` refuses an existing name: drop our own previous entry first (no-op when absent).
    run([claude, "mcp", "remove", "--scope", scope, SERVER_NAME], capture_output=True, text=True, check=False)
    res = run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise InstallError(f"`claude mcp add` failed ({res.returncode}): {(res.stderr or res.stdout).strip()[:300]}")
    return cmd


def next_step_for_clients(repo_root: Path) -> str | None:
    """The exact next command for the Claude client found on this machine (used by `doctor`), or None."""
    reg = desktop_registered()
    if reg is False:
        return "garmin-coach install claude-desktop   (then restart Claude Desktop)"
    if reg is True:
        return "restart Claude Desktop and ask: \"How should I train today?\""
    if claude_cli():
        return f"open Claude Code in {repo_root} (.mcp.json) or run: garmin-coach install claude-code --scope user"
    return None
