"""The web application: upload page, dashboard and per-app detail view.

Deliberately server-rendered with a little polling JavaScript. There is no
build step for this UI, which means the launcher can never be broken by the
same npm problems it exists to absorb.
"""
from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, db, deployer, errors, naming, ports, runtime

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_dirs()
    db.init()
    yield


app = FastAPI(
    title="App Launcher", docs_url=None, redoc_url=None, lifespan=lifespan
)


def _fmt_time(value: float | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value).strftime("%d %b %Y, %H:%M")


templates.env.filters["datetime"] = _fmt_time


def _app_url(row) -> str | None:
    if not row["host_port"]:
        return None
    return f"http://{config.PUBLIC_HOST}:{row['host_port']}"


def _require_app(name: str):
    row = db.get_app_by_name(name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No app named {name}")
    return row


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    rows = db.list_apps()
    apps = []
    for row in rows:
        latest = db.list_deploys(int(row["id"]), limit=1)
        apps.append(
            {
                "row": row,
                "url": _app_url(row),
                "latest": latest[0] if latest else None,
            }
        )
    return templates.TemplateResponse(
        request, "index.html",
        {"apps": apps, "docker_ok": runtime.docker_available()},
    )


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
    state = runtime.container_state(row["container_id"] or "")
    return templates.TemplateResponse(
        request, "detail.html",
        {
            "app": row,
            "url": _app_url(row),
            "deploys": deploys,
            "current": current,
            "container_state": state,
        },
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
    runtime.stop_container(row["container_id"] or "", remove=False)
    db.update_app(int(row["id"]), status="stopped")
    return RedirectResponse(f"/app/{name}", status_code=303)


@app.post("/app/{name}/start")
def start_app(name: str):
    row = _require_app(name)
    container = row["container_id"] or ""
    if container:
        try:
            runtime._run(["docker", "start", container])
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
    runtime.stop_container(row["container_id"] or "")
    if row["image_tag"]:
        runtime.remove_image(row["image_tag"])
    ports.release(int(row["id"]))
    db.delete_app(int(row["id"]))
    shutil.rmtree(config.SRC_DIR / name, ignore_errors=True)
    shutil.rmtree(config.UPLOAD_DIR / name, ignore_errors=True)
    shutil.rmtree(config.LOG_DIR / name, ignore_errors=True)
    return RedirectResponse("/", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True, "docker": runtime.docker_available(), "apps": len(db.list_apps())}


def _error_page(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html", {"message": message}, status_code=400
    )
