"""Central configuration. Everything is overridable by environment variable so
the same code runs on the server and on a laptop for development."""
from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# Where everything lives on disk. One directory to back up, one to blow away.
DATA_DIR = _env_path("LAUNCHER_DATA_DIR", "/var/lib/applauncher")
UPLOAD_DIR = DATA_DIR / "uploads"    # every ZIP ever uploaded, for rollback
SRC_DIR = DATA_DIR / "src"           # extracted source of the live version
LOG_DIR = DATA_DIR / "logs"          # build + runtime logs per deploy
DB_PATH = DATA_DIR / "launcher.db"

# Host ports handed out to apps. Firewall this range to the LAN.
PORT_RANGE_START = _env_int("LAUNCHER_PORT_START", 20000)
PORT_RANGE_END = _env_int("LAUNCHER_PORT_END", 29999)

# Builds are the memory hogs: ~2GB each. On a 16GB box, two at a time.
MAX_CONCURRENT_BUILDS = _env_int("LAUNCHER_MAX_BUILDS", 2)
BUILD_TIMEOUT_SECONDS = _env_int("LAUNCHER_BUILD_TIMEOUT", 900)

# Per-app runtime caps, passed to `docker run`. One runaway app must not take
# down everyone else's.
APP_MEMORY_LIMIT = os.environ.get("LAUNCHER_APP_MEMORY", "1g")
APP_CPU_LIMIT = os.environ.get("LAUNCHER_APP_CPUS", "1.0")

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
