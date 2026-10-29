from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from garmin_coach import install

UV = str(Path("/opt/uv/bin/uv"))


def test_desktop_install_keeps_other_servers_and_backs_up(tmp_path: Path) -> None:
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir()
    other = {"mcpServers": {"other": {"command": "x", "env": {"TOKEN": "keep-me"}}}, "preferences": {"a": 1}}
    cfg.write_text(json.dumps(other), encoding="utf-8")
    repo = tmp_path / "repo"
    dry = install.install_claude_desktop(repo, dry_run=True, path=cfg, uv=UV)
    assert dry.changed and json.loads(cfg.read_text(encoding="utf-8")) == other  # dry run writes nothing
    res = install.install_claude_desktop(repo, path=cfg, uv=UV)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["preferences"] == {"a": 1} and data["mcpServers"]["other"] == other["mcpServers"]["other"]
    entry = data["mcpServers"]["garmin-coach"]
    assert entry == {"command": UV, "args": ["run", "--project", str(repo.resolve()), "garmin-coach", "mcp"]}
    assert "env" not in entry  # never secrets in the generated config
    assert res.backup is not None and json.loads(res.backup.read_text(encoding="utf-8")) == other
    assert not install.install_claude_desktop(repo, path=cfg, uv=UV).changed  # idempotent
    assert install.desktop_registered(cfg) is True


def test_desktop_install_creates_missing_file_and_refuses_invalid_json(tmp_path: Path) -> None:
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    assert install.desktop_registered(cfg) is None  # Claude Desktop not installed
    res = install.install_claude_desktop(tmp_path, path=cfg, uv=UV)
    assert res.backup is None and "garmin-coach" in json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]
    cfg.write_text("{broken", encoding="utf-8")
    with pytest.raises(install.InstallError, match="not valid JSON"):
        install.install_claude_desktop(tmp_path, path=cfg, uv=UV)
    assert cfg.read_text(encoding="utf-8") == "{broken"


def test_windows_paths_are_escaped_in_json(tmp_path: Path) -> None:
    cfg = tmp_path / "c.json"
    install.install_claude_desktop(Path(r"C:\dev\garmin-coach"), path=cfg, uv=r"C:\Tools\uv.exe")
    text = cfg.read_text(encoding="utf-8")
    assert r"C:\\Tools\\uv.exe" in text and json.loads(text)["mcpServers"]["garmin-coach"]["command"] == r"C:\Tools\uv.exe"


def test_claude_code_install_replaces_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "claude_cli", lambda: "claude")
    monkeypatch.setattr(install, "uv_path", lambda: UV)
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    cmd = install.install_claude_code(tmp_path, "user", run=run)
    assert calls[0][:3] == ["claude", "mcp", "remove"] and calls[1] == cmd
    assert cmd[:7] == ["claude", "mcp", "add", "--scope", "user", "garmin-coach", "--"]
    assert cmd[7:] == [UV, "run", "--project", str(tmp_path.resolve()), "garmin-coach", "mcp"]
    with pytest.raises(install.InstallError, match="scope"):
        install.install_claude_code(tmp_path, "global", run=run)


def test_claude_code_install_without_cli_prints_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "claude_cli", lambda: None)
    monkeypatch.setattr(install, "uv_path", lambda: UV)
    with pytest.raises(install.InstallError, match="claude mcp add --scope user garmin-coach"):
        install.install_claude_code(tmp_path)


def test_committed_mcp_json_uses_the_repo_project() -> None:
    repo = Path(__file__).resolve().parents[1]
    entry = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["garmin-coach"]
    assert entry["args"] == ["run", "--project", ".", "garmin-coach", "mcp"] and "env" not in entry
    skill = (repo / ".claude" / "skills" / "coach" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: coach\n") and "get_daily_brief" in skill
