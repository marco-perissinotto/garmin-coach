"""The personal profile folder and its optional link to a folder shared between machines (PLAN §5.1, D-085..D-089).

The profile is the handful of text files the owner maintains (``PROFILE_FILES``). By default it lives in
``<app data>/config``; `garmin-coach profile link <folder>` makes every machine read and write it in a folder kept in
sync by a cloud client (Google Drive, iCloud, ...). The database, the FIT files and the API key never move there.
Pure module: no MCP imports, no network.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .settings import PROFILE_FILES, Paths

# What sync clients put between the name and the extension of the copy they keep when two machines wrote the same
# file: "name (1).yaml" (Google Drive), "name (conflicted copy).md" (Dropbox), "name-HOST.yaml" (OneDrive),
# "name 2.md" (iCloud).
_CONFLICT_SEPARATORS = " (-_"


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class ProfileStatus:
    path: Path
    shared: bool
    reachable: bool
    conflicts: tuple[str, ...] = ()

    def problems(self) -> list[str]:
        """Lines for the brief flags and the tools' ``problems`` (Italian, like the other input problems)."""
        if self.shared and not self.reachable:
            return [f"cartella condivisa del profilo non raggiungibile ({self.path}): avviare il client di "
                    "sincronizzazione; fino ad allora mancano profilo, eventi, storia e regole"]
        if self.conflicts:
            return ["possibili copie in conflitto nella cartella del profilo (" + ", ".join(self.conflicts)
                    + "): tenere una sola versione per file"]
        return []

    def as_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "shared": self.shared, "reachable": self.reachable,
                "conflicts": list(self.conflicts)}


def conflict_copies(folder: Path) -> list[str]:
    """Files that look like a sync client's second copy of a profile file. Never raises."""
    known = [(Path(n).stem, Path(n).suffix) for n in PROFILE_FILES]
    try:
        names = sorted(e.name for e in os.scandir(folder) if e.is_file())
    except OSError:
        return []
    out = []
    for name in names:
        if name in PROFILE_FILES:
            continue
        stem, suffix = Path(name).stem.lower(), Path(name).suffix.lower()
        for k_stem, k_suffix in known:
            rest = stem[len(k_stem):]
            if suffix == k_suffix and stem.startswith(k_stem) and rest and rest[0] in _CONFLICT_SEPARATORS:
                out.append(name)
                break
    return out


def status(paths: Paths) -> ProfileStatus:
    shared = paths.shared_profile
    folder = shared or paths.local_config_dir
    reachable = folder.is_dir()
    conflicts = tuple(conflict_copies(folder)) if shared is not None and reachable else ()
    return ProfileStatus(folder, shared is not None, reachable, conflicts)


def _present(folder: Path) -> list[str]:
    return [n for n in PROFILE_FILES if (folder / n).is_file()]


def _same_bytes(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


@dataclass
class LinkPlan:
    """What `profile link` would do, so the CLI can ask before copying into an empty folder."""

    target: Path
    in_target: list[str]  # profile files already in the folder: they win, nothing is copied
    in_local: list[str]  # profile files of this machine
    shadowed: list[str] = field(default_factory=list)  # local files that differ from (or are missing in) the folder

    @property
    def will_copy(self) -> bool:
        return not self.in_target and bool(self.in_local)


def plan_link(paths: Paths, folder: Path) -> LinkPlan:
    target = Path(os.path.abspath(folder.expanduser()))
    local = paths.local_config_dir
    if _inside(target, paths.home):
        raise ProfileError(f"{target} is inside the app data dir {paths.home}: choose a folder of the sync client")
    if target.exists() and not target.is_dir():
        raise ProfileError(f"{target} is a file, not a folder")
    if not target.exists() and not target.parent.is_dir():
        raise ProfileError(f"{target.parent} does not exist: is the sync client running on this machine?")
    in_target = _present(target) if target.is_dir() else []
    in_local = _present(local)
    shadowed = [n for n in in_local if not _same_bytes(local / n, target / n)] if in_target else []
    return LinkPlan(target, in_target, in_local, shadowed)


def link(paths: Paths, folder: Path) -> LinkPlan:
    """Point this machine at ``folder``. An empty folder receives this machine's files; a folder that already holds a
    profile is used as it is and the local files stay where they are, unread (D-086)."""
    plan = plan_link(paths, folder)
    target = plan.target
    if not target.exists():
        target.mkdir()
    if plan.will_copy:
        for n in plan.in_local:
            shutil.copy2(paths.local_config_dir / n, target / n)
        bad = [n for n in plan.in_local if not _same_bytes(paths.local_config_dir / n, target / n)]
        if bad:
            raise ProfileError(f"copy to {target} could not be verified for: {', '.join(bad)}; nothing was linked")
    paths.home.mkdir(parents=True, exist_ok=True)
    tmp = paths.profile_pointer.with_name(paths.profile_pointer.name + ".tmp")
    tmp.write_text(f"{target}\n", encoding="utf-8")
    os.replace(tmp, paths.profile_pointer)
    return plan


@dataclass
class UnlinkResult:
    was: Path
    copied: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)


def unlink(paths: Paths, today: date) -> UnlinkResult:
    """Go back to the local folder, taking the shared files along when the shared folder is reachable. A local file
    that differs is kept as ``<name>.bak-<date>``. The shared folder itself is left untouched."""
    shared = paths.shared_profile
    if shared is None:
        raise ProfileError("the profile is not linked to a shared folder")
    res = UnlinkResult(shared)
    local = paths.local_config_dir
    local.mkdir(parents=True, exist_ok=True)
    if shared.is_dir():
        for n in _present(shared):
            dst = local / n
            if dst.is_file() and not _same_bytes(shared / n, dst):
                backup = dst.with_name(f"{n}.bak-{today.isoformat()}")
                shutil.copy2(dst, backup)
                res.backups.append(backup.name)
            shutil.copy2(shared / n, dst)
            res.copied.append(n)
    paths.profile_pointer.unlink(missing_ok=True)
    return res
