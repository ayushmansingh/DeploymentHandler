#!/usr/bin/env python3
"""Restarts the launcher after it has replaced its own files.

The launcher cannot restart itself: the process doing the restarting has to
outlive the one being restarted. So it hands over to this, which waits for it
to exit, starts the new version, and checks that the new version actually
answers.

If it does not, the previous version is put back and started instead. That
matters more than it sounds: the launcher is the only remote way to fix the
launcher, so an update that fails to start would otherwise mean a walk to the
machine.

Not run by hand - the launcher starts it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WAIT_FOR_EXIT_SECONDS = 60
WAIT_FOR_HEALTHY_SECONDS = 75
LAUNCH_RECORD = "launch-command.json"


def log(data_dir: Path, message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    path = data_dir / "updates" / "update.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(line)


def still_running(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:  # noqa: BLE001 - psutil may be mid-reinstall
        if os.name == "nt":
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def wait_for_exit(pid: int, data_dir: Path) -> None:
    deadline = time.time() + WAIT_FOR_EXIT_SECONDS
    while time.time() < deadline:
        if not still_running(pid):
            return
        time.sleep(1)
    log(data_dir, f"Launcher {pid} did not exit; stopping it.")
    try:
        import psutil
        psutil.Process(pid).kill()
    except Exception:  # noqa: BLE001
        pass
    time.sleep(2)


def start_launcher(install: Path, data_dir: Path) -> subprocess.Popen | None:
    record_path = data_dir / LAUNCH_RECORD
    try:
        record = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log(data_dir, f"Cannot read {record_path}: {exc}")
        return None

    command = record.get("command")
    if not command:  # a record written by an older version
        command = [record["executable"], *record.get("argv", [])]
    env = {**os.environ, **record.get("env", {})}

    # Keep the new launcher's own output. Without this a failure to start is
    # completely silent, which is the worst possible time for silence.
    output_path = data_dir / "updates" / "launcher-start.log"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = open(output_path, "a", encoding="utf-8", errors="replace")
    output.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting launcher\n")
    output.flush()

    kwargs: dict = {}
    if os.name == "nt":
        # Its own console, so whoever is at the machine can still see and stop
        # it the way they did before the update.
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    else:
        kwargs["start_new_session"] = True

    log(data_dir, f"Starting: {' '.join(command)}")
    try:
        return subprocess.Popen(
            command, cwd=record.get("cwd", str(install)), env=env,
            stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            **kwargs,
        )
    except OSError as exc:
        log(data_dir, f"Could not start the launcher: {exc}")
        return None


def wait_until_healthy(port: int, data_dir: Path,
                       process: subprocess.Popen | None = None) -> bool:
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.time() + WAIT_FOR_HEALTHY_SECONDS
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        # A launcher that fails on import is dead within seconds; waiting the
        # full timeout for it just delays the roll back.
        if process is not None and process.poll() is not None:
            log(data_dir, f"The launcher exited immediately (code {process.returncode}).")
            return False
        time.sleep(2)
    return False


def roll_back(install: Path, data_dir: Path) -> bool:
    backup = data_dir / "updates" / "previous"
    if not backup.is_dir():
        log(data_dir, "No backup to restore.")
        return False

    log(data_dir, "Restoring the previous version.")
    for item in backup.iterdir():
        destination = install / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, destination, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(item, destination)
        except OSError as exc:
            log(data_dir, f"Could not restore {item.name}: {exc}")
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--install", required=True)
    parser.add_argument("--data", required=True)
    args = parser.parse_args()

    install = Path(args.install)
    data_dir = Path(args.data)

    log(data_dir, f"Update helper started for launcher {args.pid} on port {args.port}.")
    wait_for_exit(args.pid, data_dir)

    started = start_launcher(install, data_dir)
    if started is not None and wait_until_healthy(args.port, data_dir, started):
        log(data_dir, "The updated launcher is answering. Update complete.")
        return 0

    log(data_dir, "The updated launcher did not answer - rolling back. Its "
                  f"output is in {data_dir / 'updates' / 'launcher-start.log'}")
    if not roll_back(install, data_dir):
        log(data_dir, "Roll back failed. The launcher must be started at the machine.")
        return 1

    started = start_launcher(install, data_dir)
    if started is not None and wait_until_healthy(args.port, data_dir, started):
        log(data_dir, "The previous version is running again.")
        return 1

    log(data_dir, "The previous version did not start either. Needs attention at the machine.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
