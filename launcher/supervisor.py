"""Keeps native-mode apps alive.

Containers get this from Docker's restart policy and kernel memory limits.
Without containers the launcher has to do it itself:

  * restart an app whose processes have died
  * restart an app that has been over its memory limit for several checks
  * bring apps back up after the launcher (or the machine) restarts

The memory check is deliberately a soft limit with hysteresis: a dashboard
briefly spiking during a large query should not be bounced, but one leaking
steadily should not be allowed to take the whole machine down either.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import appdata, config, db, detect, metrics, native, ports, runtime

_thread: threading.Thread | None = None
_stop = threading.Event()
_lock = threading.Lock()

# app_id -> consecutive checks spent over the memory limit.
_strikes: dict[int, int] = {}


def _log(app_name: str, message: str) -> None:
    path = config.LOG_DIR / app_name / "runtime.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"[supervisor {stamp}] {message}\n")


def launch(app_row, reason: str = "") -> bool:
    """(Re)start one app's processes from what is already on disk.

    Returns False when the app cannot be started - usually because its source
    is gone, which means it needs a fresh deploy rather than a restart.
    """
    name = app_row["name"]
    src = config.SRC_DIR / name
    if not src.is_dir():
        _log(name, "Cannot start: the app's files are missing. Re-upload the ZIP.")
        db.update_app(int(app_row["id"]), status="failed")
        return False

    try:
        spec = detect.detect(src)
    except detect.DetectionError as exc:
        _log(name, f"Cannot start: {exc}")
        db.update_app(int(app_row["id"]), status="failed")
        return False

    # Clear out anything still holding the ports before rebinding them.
    stop(app_row)

    public_port = ports.allocate(int(app_row["id"]), ports.PUBLIC)
    backend_port = (
        ports.allocate(int(app_row["id"]), ports.BACKEND) if spec.backend else None
    )

    if reason:
        _log(name, reason)

    processes = native.start(
        name, src, spec, public_port, backend_port, config.LOG_DIR / name,
        appdata.dir_for(name), db.settings_env(int(app_row["id"])),
    )
    db.update_app(
        int(app_row["id"]),
        status="live",
        host_port=public_port,
        backend_port=backend_port,
        pid=processes.backend_pid,
        front_pid=processes.front_pid,
    )
    _strikes.pop(int(app_row["id"]), None)
    return True


def stop(app_row) -> None:
    """Stop an app's processes, leaving its record and ports in place."""
    native.stop(
        native.Processes(
            front_pid=app_row["front_pid"],
            backend_pid=app_row["pid"],
        )
    )
    db.update_app(int(app_row["id"]), pid=None, front_pid=None)


def _front_alive(app_row) -> bool:
    marker = f"--port {app_row['host_port']}"
    return native.is_running(app_row["front_pid"], marker)


def _backend_alive(app_row) -> bool:
    if not app_row["backend_port"]:
        return True  # this app has no backend, so nothing to miss
    return native.is_running(app_row["pid"], str(app_row["backend_port"]))


def check_once() -> None:
    """One pass over every app that is supposed to be running."""
    for row in db.list_apps():
        if row["status"] != "live":
            continue
        app_id = int(row["id"])

        if not _front_alive(row) or not _backend_alive(row):
            _log(row["name"], "App is not running any more - restarting it.")
            launch(row)
            continue

        used = native.memory_mb(row["front_pid"]) + native.memory_mb(row["pid"])
        if used > config.APP_MEMORY_LIMIT_MB:
            _strikes[app_id] = _strikes.get(app_id, 0) + 1
            if _strikes[app_id] >= config.MEMORY_STRIKES_BEFORE_RESTART:
                _log(
                    row["name"],
                    f"Using {used:.0f} MB, over the {config.APP_MEMORY_LIMIT_MB} MB "
                    "limit - restarting it so it does not affect other apps.",
                )
                launch(row)
        else:
            _strikes.pop(app_id, None)


def _is_serving(app_row) -> bool:
    """Whether this app is answering right now, under either runtime."""
    if config.RUNTIME == "native":
        return _front_alive(app_row) and _backend_alive(app_row)
    return runtime.container_state(app_row["container_id"] or "") == "running"


def recover_interrupted_deploys() -> None:
    """Resolve deploys that were still building when the launcher stopped.

    Nothing is going to finish them - the process that was doing the work is
    gone - so left alone they stay "building" forever, and the app sits on the
    dashboard spinning at something that will never happen.
    """
    for deploy in db.list_unfinished_deploys():
        app_row = db.get_app(int(deploy["app_id"]))
        name = app_row["name"] if app_row else "?"
        db.finish_deploy(
            int(deploy["id"]),
            "failed",
            "The server restarted while this was building, so it never "
            "finished. Upload the ZIP again.",
        )
        _log(name, "A build was interrupted by the server stopping.")

        if app_row is not None and app_row["status"] in ("building", "new"):
            # A previous version may still be up; the interrupted build does
            # not change that.
            serving = _is_serving(app_row)
            db.update_app(int(app_row["id"]), status="live" if serving else "failed")


def restore_on_startup() -> None:
    """Bring back apps that were running when the launcher last stopped.

    After a machine restart the recorded pids belong to nothing, so anything
    marked live is started again from its existing files. Nothing is rebuilt:
    the environment and the built frontend are already on disk, so this is a
    matter of seconds rather than minutes.
    """
    recover_interrupted_deploys()
    for row in db.list_apps():
        if row["status"] != "live":
            continue
        if _front_alive(row) and _backend_alive(row):
            continue  # survived a launcher restart; leave it alone
        launch(row, reason="Starting after the launcher restarted.")


def _loop() -> None:
    while not _stop.wait(config.SUPERVISOR_INTERVAL_SECONDS):
        try:
            metrics.sample()
            with _lock:
                check_once()
        except Exception:  # noqa: BLE001 - the supervisor must never die
            continue


def start_background() -> None:
    global _thread
    if config.RUNTIME != "native":
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="supervisor", daemon=True)
        _thread.start()


def stop_background() -> None:
    _stop.set()
