"""Central configuration. Everything is overridable by environment variable so
the same code runs on the server and on a laptop for development."""
from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


IS_WINDOWS = os.name == "nt"


def _default_data_dir() -> str:
    """Somewhere writable without administrator rights on either platform."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return str(Path(base) / "AppLauncher")
    return "/var/lib/applauncher"


# How apps are run. "native" launches them as ordinary processes and needs no
# administrator rights; "docker" gives real isolation and resource limits but
# needs Docker, which on Windows means WSL, which needs an admin to enable it.
RUNTIME = os.environ.get("LAUNCHER_RUNTIME", "native").strip().lower()


# Where everything lives on disk. One directory to back up, one to blow away.
DATA_DIR = _env_path("LAUNCHER_DATA_DIR", _default_data_dir())
UPLOAD_DIR = DATA_DIR / "uploads"    # every ZIP ever uploaded, for rollback
SRC_DIR = DATA_DIR / "src"           # extracted source of the live version
LOG_DIR = DATA_DIR / "logs"          # build + runtime logs per deploy
DB_PATH = DATA_DIR / "launcher.db"

# Host ports handed out to apps. These are the ones people put in a browser.
PORT_RANGE_START = _env_int("LAUNCHER_PORT_START", 20000)
PORT_RANGE_END = _env_int("LAUNCHER_PORT_END", 29999)

# Ports for backends in native mode. Bound to 127.0.0.1 only and reached
# through the app's front server, so they are never visible on the network.
BACKEND_PORT_START = _env_int("LAUNCHER_BACKEND_PORT_START", 30000)
BACKEND_PORT_END = _env_int("LAUNCHER_BACKEND_PORT_END", 39999)

# Builds are the memory hogs: ~2GB each. On a 16GB box, two at a time.
MAX_CONCURRENT_BUILDS = _env_int("LAUNCHER_MAX_BUILDS", 2)
BUILD_TIMEOUT_SECONDS = _env_int("LAUNCHER_BUILD_TIMEOUT", 900)

# Per-app runtime caps. Under docker these are enforced by the kernel; in
# native mode the supervisor polls memory and restarts an app that exceeds the
# limit, which is softer but still stops one runaway app from taking the
# machine down with it.
APP_MEMORY_LIMIT = os.environ.get("LAUNCHER_APP_MEMORY", "1g")
APP_CPU_LIMIT = os.environ.get("LAUNCHER_APP_CPUS", "1.0")
APP_MEMORY_LIMIT_MB = _env_int("LAUNCHER_APP_MEMORY_MB", 1024)

# How often the supervisor checks that running apps are still alive and within
# their memory limit.
SUPERVISOR_INTERVAL_SECONDS = _env_int("LAUNCHER_SUPERVISOR_INTERVAL", 15)

# An app has to exceed its memory limit on this many consecutive checks before
# it is restarted, so a brief spike during a request does not bounce it.
MEMORY_STRIKES_BEFORE_RESTART = _env_int("LAUNCHER_MEMORY_STRIKES", 3)

# npm is a .cmd shim on Windows, so it has to be resolved rather than assumed.
NPM_COMMAND = os.environ.get("LAUNCHER_NPM", "")

# Upload limits. A ZIP over this is almost always a bundled node_modules.
MAX_UPLOAD_BYTES = _env_int("LAUNCHER_MAX_UPLOAD_MB", 200) * 1024 * 1024
MAX_EXTRACTED_BYTES = _env_int("LAUNCHER_MAX_EXTRACTED_MB", 1024) * 1024 * 1024
MAX_ARCHIVE_ENTRIES = _env_int("LAUNCHER_MAX_ENTRIES", 20000)

# How many uploaded versions to keep per app before pruning the oldest.
KEEP_VERSIONS = _env_int("LAUNCHER_KEEP_VERSIONS", 5)

# The hostname users see in the links we hand out. Set this to the server's
# static LAN IP or DNS name, otherwise links only work from the server itself.
PUBLIC_HOST = os.environ.get("LAUNCHER_PUBLIC_HOST", "localhost")

# Junk we strip from every upload before building. Users WILL zip these.
JUNK_DIRS = {
    "node_modules", ".venv", "venv", "env", "__pycache__", ".git", ".next",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".DS_Store", ".idea",
    ".vscode", "coverage", ".tox", ".cache",
}

# Container-internal ports. These never collide because each container has its
# own network namespace, so every app can use the same numbers.
INTERNAL_BACKEND_PORT = 8000   # where the Python app is told to listen
INTERNAL_HTTP_PORT = 80        # nginx: serves the frontend, proxies /api


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, SRC_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
