"""Running apps as ordinary processes, with no containers and no admin rights.

Each app gets:

  * its own Python virtual environment, so backends cannot break each other's
    dependencies (the one kind of isolation that survives without containers)
  * a frontend built once to static files at deploy time
  * a front server process (launcher.appserver) on the app's public port
  * a backend process on a loopback-only port, if the app has a backend

What this deliberately does not provide, and containers would: memory and CPU
limits enforced by the kernel, a private filesystem per app, and automatic
restarts. The supervisor module reimplements the last two in a softer form.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

from . import config, settings
from .detect import Spec

LogSink = Callable[[str], None]

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ".launcher/venv"

# The port detection bakes into generated start commands. Rewritten per app at
# launch, since without network namespaces two apps cannot share a port.
PLACEHOLDER_PORT = 8000


class ToolchainError(RuntimeError):
    """Something the launcher needs is missing from the machine."""


@dataclass
class Processes:
    front_pid: int | None = None
    backend_pid: int | None = None


# ---------------------------------------------------------------------------
# Toolchain
# ---------------------------------------------------------------------------

def npm_command() -> str:
    """Resolve npm, which is a .cmd shim on Windows and not directly callable."""
    if config.NPM_COMMAND:
        return config.NPM_COMMAND
    found = shutil.which("npm") or shutil.which("npm.cmd")
    if not found:
        raise ToolchainError(
            "npm was not found on this machine, so frontends cannot be built. "
            "Install Node.js (the portable ZIP works without administrator "
            "rights) and either add it to PATH or set LAUNCHER_NPM to the full "
            "path of npm.cmd."
        )
    return found


def toolchain_report() -> dict[str, str | None]:
    """What is available for building apps. Used by the dashboard and selftest."""
    try:
        npm: str | None = npm_command()
    except ToolchainError:
        npm = None
    return {
        "python": sys.executable,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "npm": npm,
    }


def venv_python(src: Path) -> Path:
    """Path to the app venv's interpreter, on either platform."""
    venv = src / VENV_DIR
    return venv / ("Scripts/python.exe" if config.IS_WINDOWS else "bin/python")


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _stream(cmd: list[str], cwd: Path, sink: LogSink, timeout: int,
            env: dict[str, str] | None = None) -> int:
    sink(f"$ {' '.join(cmd)}\n")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
    except OSError as exc:
        sink(f"[launcher] Could not run {cmd[0]}: {exc}\n")
        return 127
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sink(line)
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        sink(f"\n[launcher] Timed out after {timeout} seconds.\n")
        return 124
    finally:
        if proc.stdout:
            proc.stdout.close()


def prepare(src: Path, spec: Spec, sink: LogSink) -> int:
    """Install dependencies and build the frontend. Returns 0 on success."""
    timeout = config.BUILD_TIMEOUT_SECONDS

    if spec.backend:
        sink("[launcher] Creating a private Python environment for this app...\n")
        code = _stream([sys.executable, "-m", "venv", str(src / VENV_DIR)], src, sink, 300)
        if code != 0:
            return code

        python = str(venv_python(src))
        if spec.backend.requirements:
            requirements = src / spec.backend.requirements
            code = _stream([python, "-m", "pip", "install", "--disable-pip-version-check",
                            "-r", str(requirements)], src, sink, timeout)
            if code != 0:
                return code

        # Safety net: generated projects routinely forget to list the server
        # they are started with, and uvicorn also serves the WSGI apps that
        # would use gunicorn on Linux (gunicorn does not run on Windows).
        code = _stream([python, "-m", "pip", "install", "--disable-pip-version-check",
                        "uvicorn"], src, sink, 300)
        if code != 0:
            return code

    if spec.frontend:
        npm = npm_command()
        fe = src / spec.frontend.path
        sink("[launcher] Installing frontend packages...\n")
        code = _stream([npm, "ci", "--no-audit", "--no-fund"], fe, sink, timeout)
        if code != 0:
            sink("[launcher] npm ci did not work, trying npm install...\n")
            code = _stream([npm, "install", "--no-audit", "--no-fund"], fe, sink, timeout)
            if code != 0:
                return code

        sink("[launcher] Building the frontend...\n")
        code = _stream([npm, "run", "build"], fe, sink, timeout)
        if code != 0:
            return code

        output = fe / spec.frontend.output
        if not output.is_dir():
            sink(
                f"\n[launcher] The build finished but produced no "
                f"{spec.frontend.output}/ folder.\n"
            )
            return 1

    return 0


def static_dir(src: Path, spec: Spec) -> Path | None:
    if not spec.frontend:
        return None
    return src / spec.frontend.path / spec.frontend.output


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def backend_command(spec: Spec, src: Path, port: int) -> list[str]:
    """Turn the detected start command into one bound to this app's own port.

    Detection writes commands against a fixed placeholder port because in a
    container that is always correct. Here every app shares one machine, so
    the port has to be rewritten - and gunicorn, which detection picks for
    Flask, does not run on Windows at all, so it is swapped for uvicorn's WSGI
    mode.
    """
    assert spec.backend is not None
    start = spec.backend.start

    if start.startswith("gunicorn"):
        target = shlex.split(start)[-1]
        return [str(venv_python(src)), "-m", "uvicorn", "--interface", "wsgi",
                "--host", "127.0.0.1", "--port", str(port), target]

    replacements = {
        f"--port {PLACEHOLDER_PORT}": f"--port {port}",
        f"0.0.0.0:{PLACEHOLDER_PORT}": f"127.0.0.1:{port}",
        f"127.0.0.1:{PLACEHOLDER_PORT}": f"127.0.0.1:{port}",
        "$PORT": str(port),
        "%PORT%": str(port),
    }
    for old, new in replacements.items():
        start = start.replace(old, new)
    start = start.replace("--host 0.0.0.0", "--host 127.0.0.1")

    parts = shlex.split(start)
    python = str(venv_python(src))
    if parts[0] == "python":
        return [python, *parts[1:]]
    if parts[0] in ("uvicorn", "gunicorn", "waitress-serve"):
        return [python, "-m", "uvicorn", *parts[1:]]
    return [python, "-m", *parts]


def _spawn(cmd: list[str], cwd: Path, log_path: Path,
           env: dict[str, str] | None = None) -> int:
    """Start a detached child, appending its output to `log_path`."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8", errors="replace")
    handle.write(f"\n--- started: {' '.join(cmd)}\n")
    handle.flush()

    kwargs: dict = {}
    if config.IS_WINDOWS:
        # Its own process group, so stopping the app does not signal the
        # launcher, and no console window pops up on the server's desktop.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=handle, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=env, **kwargs,
    )
    return proc.pid


def start(app_name: str, src: Path, spec: Spec, public_port: int,
          backend_port: int | None, log_dir: Path,
          data_dir: Path | None = None,
          app_settings: dict[str, str] | None = None) -> Processes:
    """Launch the app's processes and return their pids."""
    processes = Processes()
    runtime_log = log_dir / "runtime.log"

    if spec.backend and backend_port:
        reserved = {"PORT": str(backend_port), "PYTHONUNBUFFERED": "1"}
        if data_dir is not None:
            # Where the app should write anything it wants to keep.
            reserved["APP_DATA_DIR"] = str(data_dir)
        # The app's own settings, then ours - so a stored setting can never
        # displace the port or point the app away from its saved files.
        env = {
            **os.environ,
            **settings.environment(app_settings or {}, reserved),
        }
        processes.backend_pid = _spawn(
            backend_command(spec, src, backend_port),
            cwd=src / spec.backend.path,
            log_path=runtime_log,
            env=env,
        )

    static = static_dir(src, spec)
    front_cmd = [
        sys.executable, "-m", "launcher.appserver",
        "--port", str(public_port),
        "--backend-port", str(backend_port or 0),
    ]
    if static is not None:
        front_cmd += ["--static", str(static)]

    processes.front_pid = _spawn(
        front_cmd,
        cwd=REPO_ROOT,
        log_path=runtime_log,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return processes


# ---------------------------------------------------------------------------
# Process control
# ---------------------------------------------------------------------------

def _kill_tree(pid: int) -> None:
    """Kill a process and its children.

    Children matter: npm and uvicorn both spawn workers that outlive the
    parent and would keep the port bound, so the next deploy would fail to
    bind with no obvious cause.
    """
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    children = parent.children(recursive=True)
    for proc in (*children, parent):
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            continue

    _, alive = psutil.wait_procs((*children, parent), timeout=10)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue


def is_running(pid: int | None, marker: str | None = None) -> bool:
    """Whether `pid` is alive and, if given, still the process we started.

    The marker guards against PID reuse: the operating system will hand our
    recorded number to an unrelated process eventually, and reporting someone
    else's process as a healthy app would hide a real outage.
    """
    if not pid:
        return False
    try:
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if marker is None:
            return True
        return marker in " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def stop(processes: Processes) -> None:
    for pid in (processes.front_pid, processes.backend_pid):
        if pid:
            _kill_tree(pid)


def memory_mb(pid: int | None) -> float:
    """Resident memory of a process and its children, in megabytes."""
    if not pid:
        return 0.0
    try:
        proc = psutil.Process(pid)
        total = proc.memory_info().rss
        for child in proc.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.NoSuchProcess:
                continue
        return total / 1024 / 1024
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0.0
