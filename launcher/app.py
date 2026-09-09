"""The web application: upload page, dashboard and per-app detail view.

Deliberately server-rendered with a little polling JavaScript. There is no
build step for this UI, which means the launcher can never be broken by the
same npm problems it exists to absorb.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import appdata, config, db, deployer, errors, files, naming, native, ports
from . import runtime
from . import supervisor

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_dirs()
    db.init()
    if config.RUNTIME == "native":
        # Apps do not survive a machine restart on their own, so bring back
        # whatever was running before, then keep watching them.
        supervisor.restore_on_startup()
        supervisor.start_background()
    yield
    supervisor.stop_background()


app = FastAPI(
    title="App Launcher", docs_url=None, redoc_url=None, lifespan=lifespan
)


def _fmt_time(value: float | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value).strftime("%d %b %Y, %H:%M")


templates.env.filters["datetime"] = _fmt_time


def runtime_state(row) -> str:
    """A one-word description of what this app's processes are doing."""
    if config.RUNTIME == "native":
        if not row["front_pid"]:
            return "stopped"
        alive = native.is_running(row["front_pid"], f"--port {row['host_port']}")
        return "running" if alive else "missing"
    return runtime.container_state(row["container_id"] or "")


def runtime_ready() -> tuple[bool, str]:
    """Whether this server can currently build and run apps at all."""
    blocking = [p for p in config.environment_problems() if "every deploy fails" in p]
    if blocking:
        return False, blocking[0]
    if config.RUNTIME == "native":
        tools = native.toolchain_report()
        if not tools["npm"]:
            return False, (
                "Node.js was not found, so apps with a frontend cannot be built. "
                "Backend-only apps still work."
            )
        return True, ""
    if not runtime.docker_available():
        return False, "Docker is not running on this server."
    return True, ""


def _app_url(row) -> str | None:
    if not row["host_port"]:
        return None
    return f"http://{config.PUBLIC_HOST}:{row['host_port']}"


def _require_app(name: str):
    row = db.get_app_by_name(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No app named {name}")
    return row


# Order the dashboard puts apps in: what is running first, what needs
# attention last, so the useful links are always at the top of the page.
_STATUS_ORDER = {"live": 0, "building": 1, "stopped": 2, "new": 3, "failed": 4}


def _memory_mb(row) -> float | None:
    """Resident memory for an app, when the runtime can tell us cheaply."""
    if config.RUNTIME != "native" or row["status"] != "live":
        return None
    used = native.memory_mb(row["front_pid"]) + native.memory_mb(row["pid"])
    return round(used) if used else None


def _app_summary(row) -> dict:
    """Everything the dashboard shows about one app."""
    latest = db.list_deploys(int(row["id"]), limit=1)
    deploy = latest[0] if latest else None
    return {
        "name": row["name"],
        "status": row["status"],
        "kind": row["kind"],
        "owner": row["owner"] or "",
        "url": _app_url(row),
        "port": row["host_port"],
        "memory_mb": _memory_mb(row),
        "deployed_at": deploy["created_at"] if deploy else None,
        "data_size": appdata.human_size(appdata.size_bytes(row["name"])),
        "deploy_status": deploy["status"] if deploy else None,
        "error": deploy["error_summary"] if deploy else None,
    }


def _all_summaries() -> list[dict]:
    summaries = [_app_summary(row) for row in db.list_apps()]
    summaries.sort(key=lambda a: (_STATUS_ORDER.get(a["status"], 9), a["name"]))
    return summaries


def _grid_context() -> dict:
    """Context for the app grid, including whether anything is still working."""
    apps = _all_summaries()
    return {
        "apps": apps,
        "live_count": sum(1 for a in apps if a["status"] == "live"),
        "busy": any(
            a["status"] in ("building", "new") or a["deploy_status"] in ("queued", "building")
            for a in apps
        ),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    ready, problem = runtime_ready()
    return templates.TemplateResponse(
        request, "index.html",
        {**_grid_context(), "runtime_ready": ready, "runtime_problem": problem},
    )


@app.get("/partials/apps", response_class=HTMLResponse)
def partial_apps(request: Request):
    """The app grid on its own, for the dashboard's live refresh."""
    return templates.TemplateResponse(request, "_apps.html", _grid_context())


@app.get("/api/apps")
def api_apps():
    """Backs the dashboard's live refresh, so a building app updates in place."""
    apps = _all_summaries()
    return {
        "apps": apps,
        "live_count": sum(1 for a in apps if a["status"] == "live"),
    }


class UploadTooLarge(Exception):
    """The uploaded file exceeded the configured cap mid-stream."""


async def _store_upload(app_name: str, file: UploadFile, stamp: str) -> Path:
    """Stream an upload to disk under a hard size cap.

    Written incrementally rather than read into memory, so a 2GB upload from
    someone who zipped their node_modules cannot exhaust RAM or fill the disk
    before we get a chance to reject it.
    """
    dest_dir = config.UPLOAD_DIR / app_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / f"{stamp}.zip"

    written = 0
    try:
        with open(zip_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > config.MAX_UPLOAD_BYTES:
                    raise UploadTooLarge
                out.write(chunk)
    except UploadTooLarge:
        zip_path.unlink(missing_ok=True)
        raise
    return zip_path


def _queue_deploy(app_id: int, app_name: str, zip_path: Path, uploaded_by: str,
                  stamp: str) -> int:
    log_path = config.LOG_DIR / app_name / f"{stamp}.log"
    deploy_id = db.create_deploy(
        app_id, str(zip_path), uploaded_by=uploaded_by, log_path=str(log_path)
    )
    deployer.enqueue(deploy_id)
    return deploy_id


def _too_large_message() -> str:
    limit_mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
    return (
        f"That ZIP is larger than {limit_mb} MB. This almost always means it "
        "contains a node_modules folder \u2014 please zip only your source code."
    )


@app.post("/upload")
async def upload(
    request: Request,
    name: str = Form(...),
    uploaded_by: str = Form(""),
    file: UploadFile = None,  # type: ignore[assignment]
):
    if file is None or not file.filename:
        return _error_page(request, "Please choose a ZIP file to upload.")

    try:
        app_name = naming.normalise(name)
    except naming.InvalidName as exc:
        return _error_page(request, str(exc))

    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    try:
        zip_path = await _store_upload(app_name, file, stamp)
    except UploadTooLarge:
        return _error_page(request, _too_large_message())

    row = db.get_app_by_name(app_name)
    app_id = int(row["id"]) if row else db.create_app(app_name, owner=uploaded_by.strip())

    deploy_id = _queue_deploy(app_id, app_name, zip_path, uploaded_by.strip(), stamp)
    return RedirectResponse(f"/app/{app_name}?deploy={deploy_id}", status_code=303)


@app.post("/app/{name}/replace")
async def replace_app(
    request: Request,
    name: str,
    uploaded_by: str = Form(""),
    stop_current: str = Form(""),
    file: UploadFile = None,  # type: ignore[assignment]
):
    """Replace a running app with a newer ZIP, keeping its name and link.

    By default the current version keeps serving while the new one builds and
    is only swapped out once the new one is proven to answer HTTP, so a broken
    upload cannot take a working dashboard offline. Ticking "stop_current"
    takes it down first, which is what you want when the old and new versions
    cannot both hold the same resource - a file, a database, a device.
    """
    row = _require_app(name)
    if file is None or not file.filename:
        return _error_page(request, "Please choose a ZIP file to upload.")

    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    try:
        zip_path = await _store_upload(name, file, stamp)
    except UploadTooLarge:
        return _error_page(request, _too_large_message())

    if stop_current:
        runtime.stop_container(row["container_id"] or "", remove=False)
        db.update_app(int(row["id"]), status="stopped")

    uploader = uploaded_by.strip() or (row["owner"] or "")
    deploy_id = _queue_deploy(int(row["id"]), name, zip_path, uploader, stamp)
    return RedirectResponse(f"/app/{name}?deploy={deploy_id}", status_code=303)


@app.get("/app/{name}", response_class=HTMLResponse)
def app_detail(request: Request, name: str, deploy: int | None = None):
    row = _require_app(name)
    deploys = db.list_deploys(int(row["id"]), limit=10)
    current = db.get_deploy(deploy) if deploy else (deploys[0] if deploys else None)
    state = runtime_state(row)
    return templates.TemplateResponse(
        request, "detail.html",
        {
            "app": row,
            "url": _app_url(row),
            "deploys": deploys,
            "current": current,
            "container_state": state,
            "data_size": appdata.human_size(appdata.size_bytes(name)),
            "data_dir": appdata.dir_for(name),
        },
    )


@app.get("/app/{name}/partial-status", response_class=HTMLResponse)
def partial_status(request: Request, name: str, deploy: int | None = None):
    """Just the part of an app's page that changes while a deploy runs."""
    row = _require_app(name)
    current = db.get_deploy(deploy) if deploy else None
    if current is None:
        recent = db.list_deploys(int(row["id"]), limit=1)
        current = recent[0] if recent else None
    return templates.TemplateResponse(
        request, "_appstatus.html",
        {"app": row, "url": _app_url(row), "current": current},
    )


@app.get("/app/{name}/log", response_class=PlainTextResponse)
def app_log(name: str, deploy: int | None = None):
    row = _require_app(name)
    target = db.get_deploy(deploy) if deploy else None
    if target is None:
        recent = db.list_deploys(int(row["id"]), limit=1)
        target = recent[0] if recent else None
    if target is None or not target["log_path"]:
        return PlainTextResponse("No log yet.")
    try:
        return PlainTextResponse(Path(target["log_path"]).read_text(errors="replace"))
    except FileNotFoundError:
        return PlainTextResponse("No log yet.")


@app.get("/app/{name}/status")
def app_status(name: str, deploy: int | None = None):
    row = _require_app(name)
    target = db.get_deploy(deploy) if deploy else None
    if target is None:
        recent = db.list_deploys(int(row["id"]), limit=1)
        target = recent[0] if recent else None
    return {
        "app_status": row["status"],
        "deploy_status": target["status"] if target else "unknown",
        "url": _app_url(row),
        "error": target["error_summary"] if target else None,
    }


@app.get("/app/{name}/repair-prompt", response_class=PlainTextResponse)
def repair_prompt(name: str, deploy: int | None = None):
    """The paste-into-Claude text behind the 'Copy error for AI' button."""
    row = _require_app(name)
    target = db.get_deploy(deploy) if deploy else None
    if target is None:
        recent = db.list_deploys(int(row["id"]), limit=1)
        target = recent[0] if recent else None
    if target is None:
        return PlainTextResponse("Nothing to report yet.")

    log_text = ""
    if target["log_path"]:
        try:
            log_text = Path(target["log_path"]).read_text(errors="replace")
        except FileNotFoundError:
            pass
    diagnosis = errors.diagnose(log_text)
    if target["error_summary"]:
        diagnosis.summary = target["error_summary"]
    return PlainTextResponse(errors.repair_prompt(name, diagnosis, log_text))


@app.post("/app/{name}/stop")
def stop_app(name: str):
    row = _require_app(name)
    # Set the status first: the supervisor only tends apps marked live, so
    # this stops it deciding the app has crashed and starting it again.
    db.update_app(int(row["id"]), status="stopped")
    if config.RUNTIME == "native":
        supervisor.stop(row)
    else:
        runtime.stop_container(row["container_id"] or "", remove=False)
    return RedirectResponse(f"/app/{name}", status_code=303)


@app.post("/app/{name}/start")
def start_app(name: str):
    row = _require_app(name)
    if config.RUNTIME == "native":
        supervisor.launch(row, reason="Started from the dashboard.")
    elif row["container_id"]:
        try:
            runtime._run(["docker", "start", row["container_id"]])
            db.update_app(int(row["id"]), status="live")
        except runtime.DockerError:
            db.update_app(int(row["id"]), status="failed")
    return RedirectResponse(f"/app/{name}", status_code=303)


@app.post("/app/{name}/redeploy/{deploy_id}")
def rollback(name: str, deploy_id: int):
    """Re-run a previous upload. This is the team's undo button."""
    row = _require_app(name)
    old = db.get_deploy(deploy_id)
    if old is None or int(old["app_id"]) != int(row["id"]):
        raise HTTPException(status_code=404, detail="Unknown version")
    if not Path(old["zip_path"]).is_file():
        raise HTTPException(status_code=410, detail="That version has been cleaned up")

    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    log_path = config.LOG_DIR / name / f"{stamp}.log"
    new_id = db.create_deploy(
        int(row["id"]), old["zip_path"],
        uploaded_by=old["uploaded_by"], log_path=str(log_path),
    )
    deployer.enqueue(new_id)
    return RedirectResponse(f"/app/{name}?deploy={new_id}", status_code=303)


@app.post("/app/{name}/delete")
def delete_app(name: str):
    row = _require_app(name)

    # Status first: the supervisor only tends apps marked live, so this stops
    # it restarting the app between here and the processes actually dying.
    db.update_app(int(row["id"]), status="stopped")
    if config.RUNTIME == "native":
        supervisor.stop(row)
    else:
        runtime.stop_container(row["container_id"] or "")
        if row["image_tag"]:
            runtime.remove_image(row["image_tag"])

    ports.release(int(row["id"]))
    db.delete_app(int(row["id"]))

    # Unhook the data link before removing the source tree: on Windows,
    # deleting a tree containing a junction can delete what it points at.
    appdata.detach(config.SRC_DIR / name)

    leftovers = [
        directory / name
        for directory in (config.SRC_DIR, config.UPLOAD_DIR, config.LOG_DIR)
        if not files.remove_tree(directory / name)
    ]
    appdata.remove(name)

    if leftovers:
        # The app is gone from the dashboard either way; say what is still on
        # disk rather than leaving it to be discovered later.
        return _notice_page(
            f"\"{name}\" was deleted, but some of its files are still on the "
            "server because something was holding them open: "
            + ", ".join(str(p) for p in leftovers)
            + ". They can be removed by hand, and will not affect anything."
        )
    return RedirectResponse("/", status_code=303)


@app.get("/healthz")
def healthz():
    ready, problem = runtime_ready()
    payload = {
        "ok": ready,
        "runtime": config.RUNTIME,
        "apps": len(db.list_apps()),
    }
    if config.RUNTIME == "native":
        payload["toolchain"] = native.toolchain_report()
    else:
        payload["docker"] = runtime.docker_available()
    if problem:
        payload["problem"] = problem
    return payload


def _notice_page(message: str) -> HTMLResponse:
    """A completed action that has something worth saying about it."""
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'>"
        "<title>App Launcher</title>"
        "<body style=\"font:15px/1.6 system-ui;margin:40px;max-width:720px\">"
        f"<p>{message}</p><p><a href='/'>Back to the dashboard</a></p>"
    )


def _error_page(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html", {"message": message}, status_code=400
    )
