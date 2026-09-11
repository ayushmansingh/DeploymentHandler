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
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import __version__
from . import appdata, config, db, deployer, errors, files, metrics, naming, native
from . import selfupdate, settings as app_settings
from . import ports
from . import runtime
from . import supervisor

BASE_DIR = Path(__file__).parent
_started_at = time.time()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_dirs()
    db.init()
    # Remember how we were started, so an update can start us the same way.
    selfupdate.record_launch_command()
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
_STATUS_ORDER = {
    "live": 0, "building": 1, "needs_setup": 2, "stopped": 3, "new": 4, "failed": 5,
}


def _app_summary(row) -> dict:
    """Everything the dashboard shows about one app."""
    latest = db.list_deploys(int(row["id"]), limit=1)
    deploy = latest[0] if latest else None
    usage = metrics.for_app(row) if config.RUNTIME == "native" else None

    summary = {
        "name": row["name"],
        "status": row["status"],
        "kind": row["kind"],
        "owner": row["owner"] or "",
        "url": _app_url(row),
        "port": row["host_port"],
        "data_size": appdata.human_size(appdata.size_bytes(row["name"])),
        "deployed_at": deploy["created_at"] if deploy else None,
        "deploy_status": deploy["status"] if deploy else None,
        "error": deploy["error_summary"] if deploy else None,
        "usage": None,
        "memory_mb": None,
        "missing_settings": [d["name"] for d in db.missing_settings(int(row["id"]))],
    }
    if usage is not None:
        # Memory is shown against the per-app limit, since that is the number
        # the supervisor acts on - not against the whole machine.
        memory_percent = min(
            100.0, usage.memory_mb / max(config.APP_MEMORY_LIMIT_MB, 1) * 100
        )
        summary["usage"] = {
            "cpu_percent": round(usage.cpu_percent, 1),
            "cpu_state": metrics.state_for(usage.cpu_percent),
            "memory_mb": round(usage.memory_mb),
            "memory_percent": round(memory_percent),
            "memory_state": metrics.state_for(memory_percent),
            "memory_limit_mb": config.APP_MEMORY_LIMIT_MB,
            "disk": metrics.human_bytes(usage.disk_bytes),
        }
        summary["memory_mb"] = summary["usage"]["memory_mb"] or None
    return summary


def _all_summaries() -> list[dict]:
    summaries = [_app_summary(row) for row in db.list_apps()]
    summaries.sort(key=lambda a: (_STATUS_ORDER.get(a["status"], 9), a["name"]))
    return summaries


def _grid_context() -> dict:
    """Context for the app grid, including whether anything is still working."""
    apps = _all_summaries()
    host = metrics.host()
    return {
        "apps": apps,
        "live_count": sum(1 for a in apps if a["status"] == "live"),
        "busy": any(
            a["status"] in ("building", "new") or a["deploy_status"] in ("queued", "building")
            for a in apps
        ),
        "host": host,
        "host_states": {
            "cpu": metrics.state_for(host["cpu_percent"]),
            "memory": metrics.state_for(host["memory_percent"]),
            "disk": metrics.state_for(host["disk_percent"]),
        },
    }


def _nav(active: str) -> dict:
    """Shared chrome: which tab is current, and the count beside Dashboard."""
    return {"active_tab": active, "nav_app_count": len(db.list_apps())}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    ready, problem = runtime_ready()
    return templates.TemplateResponse(
        request, "index.html",
        {
            **_grid_context(), **_nav("dashboard"),
            "runtime_ready": ready, "runtime_problem": problem,
        },
    )


@app.get("/deploy", response_class=HTMLResponse)
def deploy_page(request: Request):
    ready, problem = runtime_ready()
    return templates.TemplateResponse(
        request, "deploy.html",
        {
            **_nav("deploy"), "app_count": len(db.list_apps()),
            "runtime_ready": ready, "runtime_problem": problem,
        },
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
def app_detail(request: Request, name: str, deploy: int | None = None,
               setting_saved: str = "", setting_removed: str = "",
               setting_error: str = "", started: str = ""):
    row = _require_app(name)
    deploys = db.list_deploys(int(row["id"]), limit=10)
    current = db.get_deploy(deploy) if deploy else (deploys[0] if deploys else None)
    state = runtime_state(row)
    return templates.TemplateResponse(
        request, "detail.html",
        {
            **_nav("dashboard"),
            "app": row,
            "url": _app_url(row),
            "deploys": deploys,
            "current": current,
            "container_state": state,
            "data_size": appdata.human_size(appdata.size_bytes(name)),
            "data_dir": appdata.dir_for(name),
            "usage": _app_summary(row)["usage"],
            "settings": db.list_setting_keys(int(row["id"])),
            "missing_settings": db.missing_settings(int(row["id"])),
            "mask": app_settings.MASK,
            "memory_strikes": config.MEMORY_STRIKES_BEFORE_RESTART,
            "setting_saved": setting_saved,
            "setting_removed": setting_removed,
            "setting_error": setting_error,
            "setting_started": bool(started),
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


@app.post("/app/{name}/settings")
def save_setting(name: str, key: str = Form(...), value: str = Form(...),
                 updated_by: str = Form("")):
    """Store or replace one setting, then restart the app so it takes effect.

    Without the restart someone would set a key, see it listed, and watch the
    app carry on with the old value - which looks like the setting was ignored.
    """
    row = _require_app(name)
    try:
        clean_key = app_settings.clean_key(key)
        clean_value = app_settings.clean_value(value)
    except app_settings.InvalidSetting as exc:
        return RedirectResponse(
            f"/app/{name}?setting_error={quote(str(exc))}", status_code=303
        )

    db.set_setting(int(row["id"]), clean_key, clean_value, updated_by.strip())
    started = _apply_settings_change(db.get_app_by_name(name))
    return RedirectResponse(
        f"/app/{name}?setting_saved={quote(clean_key)}"
        + ("&started=1" if started else ""),
        status_code=303,
    )


@app.post("/app/{name}/settings/{key}/delete")
def remove_setting(name: str, key: str):
    row = _require_app(name)
    db.delete_setting(int(row["id"]), key)
    _apply_settings_change(db.get_app_by_name(name))
    return RedirectResponse(
        f"/app/{name}?setting_removed={quote(key)}", status_code=303
    )


def _settle_held_deploy(app_id: int) -> None:
    """Close off the deploy that was waiting, now that the app has started.

    Left alone it stays "needs_setup" with its old message, and the page keeps
    saying the app is waiting for something it already has.
    """
    recent = db.list_deploys(app_id, limit=1)
    if recent and recent[0]["status"] == "needs_setup":
        db.finish_deploy(int(recent[0]["id"]), "live", None)


def _apply_settings_change(row) -> bool:
    """Bring the app into line with its settings. True if it started.

    Three cases: it was waiting to be configured and now can run; it is
    running and needs the new value; or it is running but a setting it
    declared has just been removed, in which case it stops rather than
    carrying on in a state it said it could not work in.
    """
    if row is None:
        return False
    app_id = int(row["id"])
    missing = db.missing_settings(app_id)

    if row["status"] == "needs_setup":
        if missing:
            return False
        if config.RUNTIME == "native":
            supervisor.launch(row, reason="Starting now that its settings are set.")
            _settle_held_deploy(app_id)
            return True
        return False

    if row["status"] != "live":
        return False

    if missing:
        # It declared this setting; running without it is the state we avoid.
        if config.RUNTIME == "native":
            supervisor.stop(row)
        else:
            runtime.stop_container(row["container_id"] or "", remove=False)
        db.update_app(app_id, status="needs_setup")
        return False

    # A held deploy means the version on disk is newer than the one running,
    # so this starts the new version rather than merely restarting the old.
    held = db.list_deploys(app_id, limit=1)
    taking_over = bool(held and held[0]["status"] == "needs_setup")
    reason = (
        "Starting the new version now that its settings are set."
        if taking_over
        else "Restarted to pick up a changed setting."
    )

    if config.RUNTIME == "native":
        supervisor.launch(row, reason=reason)
    else:
        try:
            runtime._run(["docker", "restart", row["container_id"] or ""])
        except runtime.DockerError:
            pass
    _settle_held_deploy(app_id)
    return taking_over


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
    metrics.forget(name)

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


def _update_log() -> str:
    try:
        text = (config.DATA_DIR / "updates" / "update.log").read_text(errors="replace")
    except FileNotFoundError:
        return ""
    return "\n".join(text.splitlines()[-40:])


@app.get("/admin/update", response_class=HTMLResponse)
def admin_update(request: Request, message: str = "", kind: str = "ok"):
    return templates.TemplateResponse(
        request, "admin.html",
        {
            **_nav("server"),
            "version": __version__,
            "started_at": _started_at,
            "install_dir": selfupdate.INSTALL_DIR,
            "app_count": len(db.list_apps()),
            "update_log": _update_log(),
            "message": message,
            "message_kind": kind,
        },
    )


@app.post("/admin/update")
async def apply_update(request: Request, file: UploadFile = None):  # type: ignore[assignment]
    """Replace the launcher with an uploaded copy of itself, then restart.

    Everything that can be checked is checked before a single file is
    replaced, because this is the one upload that can make the server
    unreachable from here.
    """
    if file is None or not file.filename:
        return RedirectResponse(
            "/admin/update?message=Please+choose+a+ZIP+file.&kind=bad", status_code=303
        )

    config.ensure_dirs()
    incoming = config.DATA_DIR / "updates" / "upload.zip"
    incoming.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(incoming, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > config.MAX_UPLOAD_BYTES:
                out.close()
                incoming.unlink(missing_ok=True)
                return RedirectResponse(
                    "/admin/update?message=That+ZIP+is+too+large.&kind=bad",
                    status_code=303,
                )
            out.write(chunk)

    try:
        staged = selfupdate.stage(incoming)
    except selfupdate.UpdateError as exc:
        return templates.TemplateResponse(
            request, "admin.html",
            {
                **_nav("server"),
                "version": __version__, "started_at": _started_at,
                "install_dir": selfupdate.INSTALL_DIR,
                "app_count": len(db.list_apps()), "update_log": _update_log(),
                "message": str(exc), "message_kind": "bad",
            },
            status_code=400,
        )

    selfupdate.back_up()
    selfupdate.apply(staged)

    notes: list[str] = []
    if staged.requirements_changed:
        selfupdate.install_requirements(notes.append)

    port = request.url.port or 8080
    selfupdate.restart(port)
    selfupdate.stop_self()

    return HTMLResponse(_restarting_page(port))


def _restarting_page(port: int) -> str:
    """Shown while the launcher is being replaced, and reloads when it returns."""
    return f"""<!doctype html><meta charset="utf-8">
<title>Updating the launcher</title>
<body style="font:15px/1.6 system-ui;margin:0;background:#f9f9f7;color:#0b0b0b">
<div style="max-width:560px;margin:80px auto;padding:28px;background:#fcfcfb;
     border:1px solid rgba(11,11,11,.1);border-radius:12px">
  <h1 style="font-size:20px;margin:0 0 10px">Updating the launcher</h1>
  <p id="status">The new version has been installed and the launcher is
     restarting. This page will come back on its own in about 20 seconds.</p>
  <p style="color:#52514e;font-size:13.5px">Your applications are still running -
     they are not affected by this.</p>
</div>
<script>
// Poll until the new launcher answers, then go back to the update page. If it
// never answers the helper restores the previous version, which answers here
// just the same.
let tries = 0;
async function check() {{
  tries += 1;
  try {{
    const r = await fetch("/healthz", {{ cache: "no-store" }});
    if (r.ok) {{ location.href = "/admin/update?message=Update+complete."; return; }}
  }} catch (err) {{ /* still down, expected */ }}
  if (tries > 60) {{
    document.getElementById("status").textContent =
      "The launcher has not come back after two minutes. Check the server's "
      + "console window, or the update log at the machine.";
    return;
  }}
  setTimeout(check, 2000);
}}
setTimeout(check, 4000);
</script>
</body>"""


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
        request, "error.html", {**_nav("deploy"), "message": message},
        status_code=400,
    )
