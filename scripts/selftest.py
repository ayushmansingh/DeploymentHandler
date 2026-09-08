#!/usr/bin/env python3
"""End-to-end check that this install can actually deploy an app.

Builds a small but real FastAPI + Vite project, pushes it through the whole
pipeline, and confirms that the served result answers on both routes:

    GET /          -> the built frontend        (static files were copied)
    GET /api/ping  -> the Python backend        (nginx proxying works)

Those two together are the thing worth proving. Everything else in the
launcher is covered by the unit tests, which need no Docker.

    python3 scripts/selftest.py             # full check, installs from npm
    python3 scripts/selftest.py --quick     # skip the npm registry install
    python3 scripts/selftest.py --keep      # leave the app running to poke at

Exits 0 if the install is healthy, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP_NAME = "launcher-selftest"
MARKER = "launcher-selftest-marker"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""


def ok(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {msg}")


def bad(msg: str, fix: str | None = None) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")
    if fix:
        print(f"        {YELLOW}{fix}{RESET}")


def note(msg: str) -> None:
    print(f"  {DIM}{msg}{RESET}")


def step(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------------------
# The sample project
# --------------------------------------------------------------------------

BACKEND_MAIN = f'''\
from fastapi import FastAPI

app = FastAPI()


@app.get("/api/ping")
def ping():
    return {{"ok": True, "marker": "{MARKER}"}}
'''

INDEX_HTML = f'''\
<!doctype html>
<html><head><meta charset="utf-8"><title>Self test</title></head>
<body><h1>{MARKER}</h1><script type="module" src="/main.js"></script></body></html>
'''

MAIN_JS = 'fetch("/api/ping").then(r => r.json()).then(d => console.log(d));\n'

QUICK_BUILD = (
    "node -e \"const f=require('fs');f.mkdirSync('dist',{recursive:true});"
    "f.copyFileSync('index.html','dist/index.html');"
    "f.copyFileSync('main.js','dist/main.js')\""
)


def write_sample(root: Path, quick: bool) -> None:
    (root / "backend").mkdir(parents=True)
    (root / "backend" / "requirements.txt").write_text("fastapi==0.115.6\n")
    (root / "backend" / "main.py").write_text(BACKEND_MAIN)

    fe = root / "frontend"
    fe.mkdir(parents=True)
    (fe / "index.html").write_text(INDEX_HTML)
    (fe / "main.js").write_text(MAIN_JS)

    if quick:
        pkg = {"name": "selftest", "private": True, "scripts": {"build": QUICK_BUILD}}
    else:
        pkg = {
            "name": "selftest",
            "private": True,
            "scripts": {"build": "vite build"},
            "devDependencies": {"vite": "5.4.11"},
        }
    (fe / "package.json").write_text(json.dumps(pkg, indent=2))


def make_zip(src: Path, dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(src).as_posix())
    return dest


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_prerequisites() -> bool:
    step("Checking prerequisites")
    healthy = True

    if sys.version_info < (3, 10):
        bad(f"Python {sys.version_info.major}.{sys.version_info.minor} is too old",
            "The launcher needs Python 3.10 or newer.")
        healthy = False
    else:
        ok(f"Python {sys.version_info.major}.{sys.version_info.minor}")

    try:
        import fastapi, httpx, jinja2, multipart, psutil, yaml  # noqa: F401
        ok("Python dependencies are installed")
    except ImportError as exc:
        bad(f"A dependency is missing: {exc.name}",
            "Run: .venv/bin/pip install -r requirements.txt")
        return False

    from launcher import config

    note(f"Runtime: {config.RUNTIME}")
    if config.RUNTIME == "native":
        healthy = _check_native() and healthy
    else:
        healthy = _check_docker() and healthy

    config.ensure_dirs()
    free_gb = shutil.disk_usage(config.DATA_DIR).free / 1024**3
    needed = 20 if config.RUNTIME == "docker" else 5
    if free_gb < needed:
        bad(f"Only {free_gb:.0f} GB free where apps are stored ({config.DATA_DIR})",
            "Free some space before deploying apps.")
        healthy = False
    else:
        ok(f"{free_gb:.0f} GB free at {config.DATA_DIR}")

    return healthy


def _check_native() -> bool:
    """Native mode needs a Python that can build venvs, and npm for frontends."""
    from launcher import native

    healthy = True
    try:
        import venv  # noqa: F401
        ok("Python can create virtual environments")
    except ImportError:
        bad("The venv module is missing",
            "On Debian or Ubuntu run: sudo apt install python3-venv")
        healthy = False

    tools = native.toolchain_report()
    if tools["npm"]:
        ok(f"npm found at {tools['npm']}")
    else:
        bad("npm was not found, so apps with a frontend cannot be built",
            "Install Node.js - the portable ZIP needs no administrator rights - "
            "then add it to PATH or set LAUNCHER_NPM to the full path of npm.")
        healthy = False

    return healthy


def _check_docker() -> bool:
    if shutil.which("docker") is None:
        bad("The docker command was not found",
            "Inside WSL run: sudo apt install -y docker.io")
        return False
    ok("docker command found")

    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        bad("The Docker daemon is not reachable",
            "Run: sudo systemctl start docker  (and check your user is in the "
            "docker group: sudo usermod -aG docker $USER, then log out and in)")
        return False
    ok(f"Docker daemon {probe.stdout.strip()} is running")
    return True


def fetch(url: str, timeout: int = 10) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def run_pipeline(zip_path: Path) -> tuple[bool, int | None]:
    """Deploy the sample through the real pipeline. Returns (passed, port)."""
    from launcher import config, db, deployer, ports

    config.ensure_dirs()
    db.init()

    existing = db.get_app_by_name(APP_NAME)
    app_id = int(existing["id"]) if existing else db.create_app(APP_NAME, owner="selftest")

    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    stored = config.UPLOAD_DIR / APP_NAME
    stored.mkdir(parents=True, exist_ok=True)
    stored_zip = stored / f"{stamp}.zip"
    shutil.copy(zip_path, stored_zip)

    log_path = config.LOG_DIR / APP_NAME / f"{stamp}.log"
    deploy_id = db.create_deploy(
        app_id, str(stored_zip), uploaded_by="selftest", log_path=str(log_path)
    )

    note("Building the sample app. The first run installs packages and can "
         "take several minutes.")
    started = time.time()
    deployer.run_deploy(deploy_id)
    elapsed = time.time() - started

    deploy = db.get_deploy(deploy_id)
    if deploy["status"] != "live":
        bad(f"The sample app failed to deploy: {deploy['error_summary']}")
        print()
        print(Path(log_path).read_text(errors="replace")[-3000:])
        return False, ports.allocated_port(app_id)

    ok(f"Sample app built and started in {elapsed:.0f}s")
    return True, ports.allocated_port(app_id)


def check_serving(port: int) -> bool:
    from launcher import config

    step("Checking the running app")
    front = "nginx" if config.RUNTIME == "docker" else "the front server"
    healthy = True

    status, body = fetch(f"http://127.0.0.1:{port}/")
    if status == 200 and MARKER in body:
        ok("GET /  serves the built frontend")
    else:
        bad(f"GET / returned HTTP {status} without the expected content",
            "The frontend build output was not found. Check that the 'output' "
            "directory in launcher/detect.py matches what the build tool writes.")
        healthy = False

    status, body = fetch(f"http://127.0.0.1:{port}/api/ping")
    if status == 200 and MARKER in body:
        ok(f"GET /api/ping  reaches the Python backend through {front}")
    else:
        bad(f"GET /api/ping returned HTTP {status}: {body[:200]}",
            "The front server could not reach the app's backend. Check the "
            "app's runtime.log for why the backend exited.")
        healthy = False

    return healthy


def cleanup() -> None:
    from launcher import config, db, ports, runtime, supervisor

    row = db.get_app_by_name(APP_NAME)
    if row is None:
        return
    db.update_app(int(row["id"]), status="stopped")  # keep the supervisor off it
    if config.RUNTIME == "native":
        supervisor.stop(row)
    else:
        runtime.stop_container(row["container_id"] or "")
        if row["image_tag"]:
            runtime.remove_image(row["image_tag"])
    ports.release(int(row["id"]))
    db.delete_app(int(row["id"]))
    for directory in (config.SRC_DIR, config.UPLOAD_DIR, config.LOG_DIR):
        shutil.rmtree(directory / APP_NAME, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="skip the npm registry install (faster, tests less)")
    parser.add_argument("--keep", action="store_true",
                        help="leave the sample app running instead of removing it")
    parser.add_argument("--data-dir",
                        help="use a throwaway data directory instead of the real one")
    args = parser.parse_args()

    if args.data_dir:
        os.environ["LAUNCHER_DATA_DIR"] = args.data_dir

    print(f"{DIM}App Launcher self-test{RESET}")

    if not check_prerequisites():
        print(f"\n{RED}Self-test failed: fix the prerequisites above first.{RESET}")
        return 1

    step("Deploying a sample app")
    if args.quick:
        note("Quick mode: the frontend has no dependencies, so npm registry "
             "access is not tested.")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_sample(root / "project", quick=args.quick)
        zip_path = make_zip(root / "project", root / "sample.zip")

        try:
            deployed, port = run_pipeline(zip_path)
        except Exception as exc:  # noqa: BLE001 - report, never traceback at the user
            bad(f"The pipeline raised an unexpected error: {exc!r}")
            deployed, port = False, None

    healthy = deployed and port is not None and check_serving(port)

    if args.keep and healthy:
        print(f"\n{YELLOW}Leaving the sample app running on port {port}.{RESET}")
        print(f"Remove it later from the dashboard, or re-run without --keep.")
    else:
        step("Cleaning up")
        cleanup()
        ok("Sample app removed")

    print()
    if healthy:
        print(f"{GREEN}Self-test passed. This server can build and run uploaded apps.{RESET}")
        return 0
    print(f"{RED}Self-test failed. See the messages above.{RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
