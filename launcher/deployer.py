"""The deploy pipeline and its work queue.

    upload -> extract -> detect -> generate -> preflight -> build
           -> allocate port -> run -> verify -> live

Builds are the memory-hungry step, so they run on a small thread pool rather
than immediately on the request thread. Everything writes progress to a per-
deploy log file, which the dashboard streams live.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import archive, config, db, detect, errors, imagegen, ports, runtime

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

# Frontend sources worth scanning for a hardcoded localhost API base. Config
# files are excluded: a dev-server proxy pointing at localhost is correct.
_SCANNED_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".env"}
_SKIPPED_NAMES = re.compile(r"^(vite|next|webpack|rollup|jest|vitest)\.config\.")

VERIFY_TIMEOUT_SECONDS = 45


def executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=config.MAX_CONCURRENT_BUILDS,
                thread_name_prefix="build",
            )
        return _executor


class _Log:
    """Append-only deploy log. The dashboard tails the same file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        path.write_text("")

    def write(self, text: str) -> None:
        with self._lock, open(self.path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(text)

    def line(self, text: str) -> None:
        self.write(f"{text}\n")

    def read(self) -> str:
        try:
            return self.path.read_text(errors="replace")
        except FileNotFoundError:
            return ""


def _scan_for_localhost(root: Path, frontend_path: str) -> errors.Diagnosis | None:
    fe_root = root / frontend_path
    if not fe_root.is_dir():
        return None
    for path in fe_root.rglob("*"):
        if not path.is_file() or path.suffix not in _SCANNED_SUFFIXES:
            continue
        if _SKIPPED_NAMES.match(path.name):
            continue
        found = errors.check_localhost_leak(path.read_text(errors="ignore"))
        if found:
            rel = path.relative_to(root).as_posix()
            found.detail = f"{found.detail} in {rel}"
            return found
    return None


def _verify_http(port: int, log: _Log) -> bool:
    """Poll the published port until the app answers, or we give up."""
    url = f"http://127.0.0.1:{port}/"
    deadline = time.time() + VERIFY_TIMEOUT_SECONDS
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                log.line(f"[launcher] App answered with HTTP {resp.status}.")
                return True
        except urllib.error.HTTPError as exc:
            # Any HTTP response means the server is up; 404 on / is fine for a
            # backend-only app that only serves /api routes.
            log.line(f"[launcher] App answered with HTTP {exc.code}.")
            return True
        except Exception as exc:  # connection refused while it boots
            last_error = str(exc)
            time.sleep(1.5)
    log.line(f"[launcher] App never answered on port {port}: {last_error}")
    return False


def _fail(deploy_id: int, log: _Log, diagnosis: errors.Diagnosis) -> None:
    log.line(f"\n[launcher] FAILED: {diagnosis.summary}")
    if diagnosis.hint:
        log.line(f"[launcher] {diagnosis.hint}")
    db.finish_deploy(deploy_id, "failed", diagnosis.summary)


def run_deploy(deploy_id: int) -> None:
    """Execute one deploy end to end. Never raises; failures land in the log."""
    deploy = db.get_deploy(deploy_id)
    if deploy is None:
        return
    app = db.get_app(int(deploy["app_id"]))
    if app is None:
        return

    name = app["name"]
    log = _Log(Path(deploy["log_path"]))
    db.set_deploy_status(deploy_id, "building")
    db.update_app(app["id"], status="building")

    try:
        log.line(f"[launcher] Deploying {name}...")

        # 1. Extract and normalise.
        src = config.SRC_DIR / name
        result = archive.extract(Path(deploy["zip_path"]), src)
        if result.unwrapped_from:
            log.line(f"[launcher] Unwrapped folder '{result.unwrapped_from}' from the ZIP.")
        if result.stripped:
            log.line(f"[launcher] Removed from upload: {', '.join(result.stripped)}")

        # 2. Understand what we were given.
        spec = detect.detect(src)
        for note in spec.notes:
            log.line(f"[launcher] {note}")
        db.update_app(app["id"], kind=spec.kind)

        # 3. Preflight the mistake that costs the most time to discover late.
        if spec.frontend:
            leak = _scan_for_localhost(src, spec.frontend.path)
            if leak:
                _fail(deploy_id, log, leak)
                db.update_app(app["id"], status="failed")
                return

        # 4. Generate the container recipe and build it.
        imagegen.write_build_context(src, spec)
        tag = f"applauncher/{name}:{deploy_id}"
        log.line(f"[launcher] Building container image (this usually takes 1-3 minutes)...")
        code = runtime.build_image(src, tag, log.write)
        if code != 0:
            _fail(deploy_id, log, errors.diagnose(log.read()))
            db.update_app(app["id"], status="failed")
            return

        # 5. Allocate the app's stable port and swap the container.
        host_port = ports.allocate(app["id"])
        log.line(f"[launcher] Using port {host_port}.")

        old_container = app["container_id"]
        if old_container:
            log.line("[launcher] Stopping the previous version...")
            runtime.stop_container(old_container)
        runtime.remove_container_by_name(name)

        container_id = runtime.run_container(tag, name, host_port)
        log.line(f"[launcher] Started container {container_id[:12]}.")
        db.update_app(
            app["id"], container_id=container_id, image_tag=tag, host_port=host_port
        )

        # 6. Prove it actually serves traffic before calling it a success.
        if not _verify_http(host_port, log):
            log.write(runtime.container_logs(container_id, tail=100))
            diagnosis = errors.diagnose(log.read())
            diagnosis.summary = (
                "Your app was built successfully but did not start. " + diagnosis.summary
            )
            _fail(deploy_id, log, diagnosis)
            db.update_app(app["id"], status="failed")
            return

        url = f"http://{config.PUBLIC_HOST}:{host_port}"
        log.line(f"[launcher] SUCCESS. Your app is live at {url}")
        db.finish_deploy(deploy_id, "live")
        db.update_app(app["id"], status="live")
        _prune_old_versions(name)

    except (archive.ArchiveError, detect.DetectionError) as exc:
        _fail(deploy_id, log, errors.Diagnosis(summary=str(exc), detail=None, hint=None))
        db.update_app(app["id"], status="failed")
    except ports.NoPortsAvailable as exc:
        _fail(
            deploy_id, log,
            errors.Diagnosis(
                summary="The server has no free ports left for new apps.",
                detail=str(exc),
                hint="Ask the server administrator to remove unused apps.",
            ),
        )
        db.update_app(app["id"], status="failed")
    except Exception as exc:  # noqa: BLE001 - the queue must never die
        log.line(f"\n[launcher] Unexpected error: {exc!r}")
        _fail(
            deploy_id, log,
            errors.Diagnosis(
                summary="Something went wrong on the server while deploying.",
                detail=str(exc),
                hint="Send this log to the server administrator.",
            ),
        )
        db.update_app(app["id"], status="failed")


def _prune_old_versions(name: str) -> None:
    """Keep disk in check: retain only the most recent uploads per app."""
    app_uploads = sorted((config.UPLOAD_DIR / name).glob("*.zip"))
    for stale in app_uploads[: -config.KEEP_VERSIONS or None]:
        stale.unlink(missing_ok=True)


def enqueue(deploy_id: int) -> None:
    executor().submit(run_deploy, deploy_id)
