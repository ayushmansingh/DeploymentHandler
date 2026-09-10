"""Updating the launcher itself, from a browser on any machine on the network.

Walking to the server to unzip a folder does not scale, and the person who
would have to do it is usually the only one who can. The launcher already
accepts ZIPs and runs them; accepting one of itself is the same gesture.

The order matters, because a failed update locks out the only remote way to
fix it:

  stage -> validate -> back up -> swap -> restart -> prove it answers

If the new version does not answer after restarting, the helper puts the
backup back and starts that instead, so a bad upload costs a minute rather
than a trip to the machine.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import config
from .files import remove_tree

INSTALL_DIR = Path(__file__).resolve().parents[1]

# Never replaced by an update: the environment the launcher runs in, and the
# settings for this particular server.
PRESERVE = ("\\.venv", "deploy/settings.cmd", ".git")

# A ZIP without these is not the launcher, whatever it is.
REQUIRED = (
    "launcher/app.py",
    "launcher/deployer.py",
    "launcher/config.py",
    "requirements.txt",
)

LAUNCH_RECORD = "launch-command.json"


class UpdateError(Exception):
    """The uploaded ZIP cannot be used to update the launcher."""


@dataclass
class Staged:
    root: Path
    requirements_changed: bool


def launch_command() -> list[str]:
    """The command that would start this process again.

    `python -m uvicorn ...` cannot be rebuilt as `python <path>/uvicorn/__main__.py`:
    running a file puts its own directory first on sys.path, so uvicorn's
    logging.py shadows the standard library's and the process dies on import.
    __spec__ says whether we were run as a module, so the -m form is preserved.
    """
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    if spec is not None and getattr(spec, "name", None):
        module = spec.parent or spec.name.removesuffix(".__main__")
        return [sys.executable, "-m", module, *sys.argv[1:]]
    return [sys.executable, *sys.argv]


def record_launch_command() -> None:
    """Remember how this process was started, so it can be started again.

    Written on every startup rather than guessed at update time: the settings
    a server runs with live in its environment, and re-deriving them later
    would silently drop any that were set by hand.
    """
    config.ensure_dirs()
    record = {
        "command": launch_command(),
        "cwd": os.getcwd(),
        "env": {k: v for k, v in os.environ.items() if k.startswith("LAUNCHER_")},
    }
    (config.DATA_DIR / LAUNCH_RECORD).write_text(json.dumps(record, indent=2))


def stop_self(delay: float = 1.5) -> None:
    """Exit shortly, so the helper does not have to wait us out and kill us.

    The response has to reach the browser first, hence the delay. Exiting
    abruptly is safe here: applications are separate processes and keep
    running, and a deploy interrupted mid-build is resolved on the next start.
    """
    def quit_now() -> None:
        time.sleep(delay)
        os._exit(0)

    threading.Thread(target=quit_now, name="update-exit", daemon=True).start()


def _is_preserved(relative: str) -> bool:
    normalised = relative.replace("\\", "/")
    return any(
        normalised == keep.replace("\\", "/").strip("\\/")
        or normalised.startswith(keep.replace("\\", "/").strip("\\/") + "/")
        for keep in PRESERVE
    )


def stage(zip_path: Path) -> Staged:
    """Unpack the upload somewhere harmless and check it is what it claims."""
    if not zipfile.is_zipfile(zip_path):
        raise UpdateError("That file is not a ZIP archive.")

    staging = config.DATA_DIR / "updates" / time.strftime("%Y-%m-%d_%H-%M-%S")
    remove_tree(staging)
    staging.mkdir(parents=True)

    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if ".." in parts or name.startswith("/"):
                raise UpdateError("That ZIP contains an unsafe path and was rejected.")
            target = staging / "/".join(parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)

    # A ZIP of a folder containing the project rather than the project itself.
    entries = [p for p in staging.iterdir() if p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir() and not (staging / "launcher").is_dir():
        inner = entries[0]
        for item in list(inner.iterdir()):
            shutil.move(str(item), str(staging / item.name))
        remove_tree(inner)

    missing = [name for name in REQUIRED if not (staging / name).is_file()]
    if missing:
        raise UpdateError(
            "That ZIP does not look like the App Launcher - it is missing "
            + ", ".join(missing)
            + ". Upload the launcher's own ZIP, not an application."
        )

    _check_it_compiles(staging)

    current = (INSTALL_DIR / "requirements.txt").read_text(errors="ignore")
    incoming = (staging / "requirements.txt").read_text(errors="ignore")
    return Staged(root=staging, requirements_changed=current.strip() != incoming.strip())


def _check_it_compiles(staged: Path) -> None:
    """Refuse a version that cannot even be parsed.

    Cheap, and it catches the case that matters most: a truncated or corrupted
    upload that would leave the launcher unable to start at all.
    """
    result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(staged / "launcher")],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise UpdateError(
            "That ZIP contains Python that will not compile, so it was "
            "rejected before anything was replaced:\n"
            + (result.stdout or result.stderr).strip()[:500]
        )


def back_up() -> Path:
    """Copy the current install aside so a bad update can be undone."""
    backup = config.DATA_DIR / "updates" / "previous"
    remove_tree(backup)
    backup.mkdir(parents=True)
    for item in INSTALL_DIR.iterdir():
        if _is_preserved(item.name):
            continue
        destination = backup / item.name
        if item.is_dir():
            shutil.copytree(item, destination, symlinks=True)
        else:
            shutil.copy2(item, destination)
    return backup


def apply(staged: Staged) -> None:
    """Copy the new version over the install, keeping venv and settings."""
    for path in sorted(staged.root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(staged.root).as_posix()
        if _is_preserved(relative):
            continue
        destination = INSTALL_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def install_requirements(sink) -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "-r", str(INSTALL_DIR / "requirements.txt")],
        capture_output=True, text=True, timeout=900,
    )
    sink(result.stdout or "")
    sink(result.stderr or "")
    return result.returncode


def restart(port: int) -> None:
    """Hand over to a helper that restarts us and rolls back if we do not return."""
    helper = INSTALL_DIR / "scripts" / "update_helper.py"
    command = [
        sys.executable, str(helper),
        "--pid", str(os.getpid()),
        "--port", str(port),
        "--install", str(INSTALL_DIR),
        "--data", str(config.DATA_DIR),
    ]
    kwargs: dict = {}
    if config.IS_WINDOWS:
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        command, cwd=str(INSTALL_DIR), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kwargs,
    )
