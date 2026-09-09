"""Storage that survives a redeploy.

Each deploy replaces the app's source directory wholesale, so anything an app
writes next to its code is destroyed on the next upload. That matters: pulling
a query result from Redash into a CSV and building a dashboard on it is a
normal thing to do, and losing the CSV on every update would make the launcher
useless for it.

So every app gets a directory outside its source tree that no deploy touches:

    <DATA_DIR>/appdata/<app-name>/

It reaches the app two ways. The reliable one is the APP_DATA_DIR environment
variable, which is always set. As a convenience we also link it in at
`<backend>/data`, so code that naively writes "data/report.csv" - which is
what an AI assistant tends to produce - persists too.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import config
from .detect import Spec

LogSink = Callable[[str], None]

LINK_NAME = "data"


def dir_for(app_name: str) -> Path:
    path = config.APPDATA_DIR / app_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _make_link(link: Path, target: Path) -> bool:
    """Point `link` at `target`, without needing administrator rights.

    Windows reserves directory symlinks for administrators (or Developer
    Mode), but directory junctions are unprivileged, so use those there.
    """
    try:
        if config.IS_WINDOWS:
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, text=True, timeout=30,
            )
            return result.returncode == 0
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def attach(app_name: str, src: Path, spec: Spec, log: LogSink) -> Path:
    """Give this deploy access to the app's persistent directory.

    Returns the directory, which the caller passes to the app as APP_DATA_DIR.
    """
    store = dir_for(app_name)
    if not spec.backend:
        return store

    link = src / spec.backend.path / LINK_NAME

    # A data/ folder shipped inside the ZIP is seed data: move it into the
    # store the first time, then get out of its way. Later uploads must not
    # overwrite what the running app has since collected.
    if link.is_dir() and not link.is_symlink():
        seeded = 0
        for item in link.iterdir():
            destination = store / item.name
            if not destination.exists():
                shutil.move(str(item), str(destination))
                seeded += 1
        if seeded:
            log(f"[launcher] Copied {seeded} file(s) from the ZIP into saved data.\n")
        shutil.rmtree(link, ignore_errors=True)
    elif link.is_symlink() or link.exists():
        link.unlink(missing_ok=True)

    if not _make_link(link, store):
        # Not fatal: APP_DATA_DIR still works, so an app that reads the
        # environment variable keeps its data either way.
        log(
            "[launcher] Note: could not link data/ into the app folder. Use the "
            "APP_DATA_DIR environment variable to save files.\n"
        )
    return store


def _is_junction(path: Path) -> bool:
    """Windows junctions are not symlinks to every API that matters here."""
    if not config.IS_WINDOWS:
        return False
    if hasattr(os.path, "isjunction"):
        return os.path.isjunction(path)
    try:
        tag = getattr(os.lstat(path), "st_reparse_tag", 0)
    except OSError:
        return False
    return tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def detach(src: Path) -> None:
    """Remove any data link before the source directory is wiped.

    This is not tidiness. On Windows, deleting a tree that contains a junction
    can delete what the junction points at, which would destroy every app's
    saved data on its next deploy. Unhooking the link first makes that
    impossible.
    """
    if not src.is_dir():
        return

    candidates = [src / LINK_NAME]
    try:
        candidates += [child / LINK_NAME for child in src.iterdir() if child.is_dir()]
    except OSError:
        pass

    for candidate in candidates:
        try:
            if candidate.is_symlink():
                candidate.unlink()
            elif _is_junction(candidate):
                os.rmdir(candidate)  # removes the junction, not its target
        except OSError:
            continue


def size_bytes(app_name: str) -> int:
    store = config.APPDATA_DIR / app_name
    if not store.is_dir():
        return 0
    total = 0
    for path in store.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def human_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "empty"
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def remove(app_name: str) -> None:
    """Delete an app's saved data. Only called when the app itself is deleted."""
    shutil.rmtree(config.APPDATA_DIR / app_name, ignore_errors=True)
