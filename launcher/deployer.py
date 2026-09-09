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

from . import appdata, archive, config, db, detect, errors, files, imagegen, native
from . import ports
from . import runtime
from . import supervisor

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


def _is_serving(app_row) -> bool:
    """Whether this app is currently answering, under either runtime."""
    if config.RUNTIME == "native":
        return native.is_running(
            app_row["front_pid"], f"--port {app_row['host_port']}"
        )
    return runtime.container_state(app_row["container_id"] or "") == "running"


def _apply_failure_status(app_row, log: _Log, swapped: bool) -> None:
    """Set the app's status after a failed deploy.

    Until the container swap happens the previous version is still up and
    serving traffic, so a failed build must not report the app as down - that
    would send someone chasing an outage that never happened.
    """
    app_id = int(app_row["id"])
    if swapped:
        db.update_app(app_id, status="failed")
        return
    if _is_serving(app_row):
        log.line(
            "[launcher] Your previous version is still running - nothing was "
            "taken offline."
        )
        db.update_app(app_id, status="live")
    else:
        db.update_app(app_id, status="failed")


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

    # Until the new container is started the old one is still serving.
    swapped = False

    try:
        log.line(f"[launcher] Deploying {name}...")

        # 1. Extract and normalise. Unhook saved data first: the extract wipes
        #    the source tree, and on Windows that could otherwise follow the
        #    link and take the app's data with it.
        src = config.SRC_DIR / name
        appdata.detach(src)
        _clear_source_dir(app, src, log)
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

        # Saved files live outside the source tree, so they outlive this deploy.
        data_dir = appdata.attach(name, src, spec, log.write)

        # 3. Preflight the mistake that costs the most time to discover late.
        if spec.frontend:
            leak = _scan_for_localhost(src, spec.frontend.path)
            if leak:
                _fail(deploy_id, log, leak)
                _apply_failure_status(app, log, swapped)
                return

        # 4. Install dependencies and build, then 5. start the new version.
        if config.RUNTIME == "native":
            code = native.prepare(src, spec, log.write)
            if code != 0:
                _fail(deploy_id, log, errors.diagnose(log.read()))
                _apply_failure_status(app, log, swapped)
                return

            host_port = ports.allocate(int(app["id"]), ports.PUBLIC)
            backend_port = (
                ports.allocate(int(app["id"]), ports.BACKEND) if spec.backend else None
            )
            log.line(f"[launcher] Using port {host_port}.")

            if app["front_pid"] or app["pid"]:
                log.line("[launcher] Stopping the previous version...")
            supervisor.stop(app)

            processes = native.start(
                name, src, spec, host_port, backend_port,
                config.LOG_DIR / name, data_dir,
            )
            swapped = True
            log.line(f"[launcher] Started the app (process {processes.front_pid}).")
            db.update_app(
                int(app["id"]), host_port=host_port, backend_port=backend_port,
                pid=processes.backend_pid, front_pid=processes.front_pid,
            )
        else:
            imagegen.write_build_context(src, spec)
            tag = f"applauncher/{name}:{deploy_id}"
            log.line("[launcher] Building container image (this usually takes 1-3 minutes)...")
            code = runtime.build_image(src, tag, log.write)
            if code != 0:
                _fail(deploy_id, log, errors.diagnose(log.read()))
                _apply_failure_status(app, log, swapped)
                return

            host_port = ports.allocate(int(app["id"]), ports.PUBLIC)
            log.line(f"[launcher] Using port {host_port}.")

            if app["container_id"]:
                log.line("[launcher] Stopping the previous version...")
                runtime.stop_container(app["container_id"])
            runtime.remove_container_by_name(name)

            container_id = runtime.run_container(tag, name, host_port, data_dir)
            swapped = True
            log.line(f"[launcher] Started container {container_id[:12]}.")
            db.update_app(
                int(app["id"]), container_id=container_id, image_tag=tag,
                host_port=host_port,
            )

        # 6. Prove it actually serves traffic before calling it a success.
        if not _verify_http(host_port, log):
            log.write(_runtime_logs(name, db.get_app(int(app["id"]))))
            diagnosis = errors.diagnose(log.read())
            diagnosis.summary = (
                "Your app was built successfully but did not start. " + diagnosis.summary
            )
            _fail(deploy_id, log, diagnosis)
            if config.RUNTIME == "native":
                supervisor.stop(db.get_app(int(app["id"])))
            _apply_failure_status(app, log, swapped)
            return

        url = f"http://{config.PUBLIC_HOST}:{host_port}"
        log.line(f"[launcher] SUCCESS. Your app is live at {url}")
        db.finish_deploy(deploy_id, "live")
        db.update_app(app["id"], status="live")
        _prune_old_versions(name)

    except (archive.ArchiveError, detect.DetectionError) as exc:
        _fail(deploy_id, log, errors.Diagnosis(summary=str(exc), detail=None, hint=None))
        _apply_failure_status(app, log, swapped)
    except native.ToolchainError as exc:
        _fail(
            deploy_id, log,
            errors.Diagnosis(
                summary=str(exc),
                detail=None,
                hint="This is a problem with the server, not with your ZIP. "
                "Please tell the server administrator.",
            ),
        )
        _apply_failure_status(app, log, swapped)
    except ports.NoPortsAvailable as exc:
        _fail(
            deploy_id, log,
            errors.Diagnosis(
                summary="The server has no free ports left for new apps.",
                detail=str(exc),
                hint="Ask the server administrator to remove unused apps.",
            ),
        )
        _apply_failure_status(app, log, swapped)
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
        _apply_failure_status(app, log, swapped)


def _clear_source_dir(app_row, src: Path, log: _Log) -> None:
    """Remove the previous version's files before unpacking the new ones.

    The running version is deliberately left up while the new one builds, but
    on Windows a file it still has open cannot be deleted - a SQLite database
    the app opened inside its own folder is the usual case. Rather than fail
    the deploy, stop the app to release the handles and try again. That costs
    downtime only in the case that would otherwise have been an error.
    """
    if not src.exists() or files.remove_tree(src):
        return

    log.line(
        "[launcher] The running version has files open, so it cannot be "
        "replaced while it is up. Stopping it and continuing."
    )
    if config.RUNTIME == "native":
        supervisor.stop(app_row)
    else:
        runtime.stop_container(app_row["container_id"] or "")

    if not files.remove_tree(src):
        raise archive.ArchiveError(
            "The previous version's files could not be removed from the "
            "server, so the new version cannot be installed. Something else "
            "is holding them open. Ask the server administrator to check "
            f"{src}."
        )


def _runtime_logs(name: str, app_row) -> str:
    """The app's own output, wherever this runtime keeps it."""
    if config.RUNTIME == "native":
        try:
            text = (config.LOG_DIR / name / "runtime.log").read_text(errors="replace")
        except FileNotFoundError:
            return ""
        return "\n".join(text.splitlines()[-100:]) + "\n"
    return runtime.container_logs(app_row["container_id"] or "", tail=100)


def _prune_old_versions(name: str) -> None:
    """Keep disk in check: retain only the most recent uploads per app."""
    app_uploads = sorted((config.UPLOAD_DIR / name).glob("*.zip"))
    for stale in app_uploads[: -config.KEEP_VERSIONS or None]:
        stale.unlink(missing_ok=True)


def enqueue(deploy_id: int) -> None:
    executor().submit(run_deploy, deploy_id)
