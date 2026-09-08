# App Launcher

Drop a ZIP, get a running app on the office network.

Built for a team that writes dashboards with an AI assistant, downloads the
ZIP, and wants it running somewhere everyone can see — without learning Git,
Docker, or the command line. It runs on an ordinary Windows machine and needs
no administrator rights.

## What your team does

1. Open `http://<server>:8080/` on the LAN.
2. Type a name, choose the ZIP from their Downloads folder, click **Deploy**.
3. Watch the build log scroll.
4. Get a link like `http://192.168.1.50:24817`.

Re-uploading under the same name updates the app and **keeps the same link**.
Every upload is retained, so any previous version can be restored with one
click.

Nobody on the team ever sees a terminal or a port number they have to manage.

### Replacing a running app

Each app's page has a **Replace with a newer ZIP** form. The current version
keeps serving while the new one builds, and is swapped out only once the new
one is proven to answer HTTP — so a broken upload cannot take a working
dashboard offline. If the build fails, the dashboard says so and the old
version stays up.

Tick **Stop the current version first** when the two versions cannot both be
running at once — typically when they would contend for the same file, SQLite
database, or hardware device. That accepts downtime for the length of the
build in exchange for a clean handover.

## How it works

Each app becomes a front server on its own public port, plus a backend on a
loopback-only port that only the front server can reach:

```
   front server :24817        <- the only port on the network
     |-- /                    static frontend build   (if the app has one)
     '-- /api  ---->  127.0.0.1:30412                 (Python backend, if any)
```

Serving both halves from one origin is what makes relative `/api/...` calls
work with no per-app configuration. Ports come from a SQLite registry, so an
app keeps its link across redeploys and restarts.

Each backend runs in **its own Python virtual environment**, so one app's
dependencies cannot break another's.

The pipeline:

```
upload -> extract -> detect -> install dependencies -> preflight
       -> build frontend -> allocate ports -> start -> verify HTTP -> live
```

A supervisor thread then watches every running app: it restarts one whose
processes have died, restarts one that has been over its memory limit for
several checks running, and brings apps back after the launcher restarts.

## Setting up the server (Windows, no admin needed)

### 1. Python

Python 3.10 or newer, with the `venv` module. If `python --version` works in
PowerShell you already have it. Otherwise install from python.org **for this
user only** (uncheck "Install for all users"), or from the Microsoft Store.

### 2. Node.js — the portable ZIP, not the installer

Frontends are built with npm. The `.msi` installer needs admin; **the ZIP does
not**:

1. Download the Windows **.zip** build from nodejs.org.
2. Extract it somewhere you can write, e.g. `C:\tools\node`.
3. Either add that folder to your user PATH (Settings -> "Edit environment
   variables for your account" — no admin needed), or set `LAUNCHER_NPM` to
   the full path of `npm.cmd`.

Skip this only if nobody will deploy an app with a frontend.

### 3. Install the launcher

```powershell
git clone -b claude/shared-server-app-launcher-s0h5cx `
  https://github.com/ayushmansingh/DeploymentHandler.git C:\tools\applauncher
cd C:\tools\applauncher
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 4. Configure

Create `start-launcher.ps1` next to it:

```powershell
$env:LAUNCHER_PUBLIC_HOST = "192.168.1.50"   # the server's LAN IP
$env:LAUNCHER_DATA_DIR    = "C:\AppLauncherData"
$env:LAUNCHER_NPM         = "C:\tools\node\npm.cmd"
$env:LAUNCHER_MAX_BUILDS  = "2"
C:\tools\applauncher\.venv\Scripts\python.exe -m uvicorn launcher.app:app `
  --host 0.0.0.0 --port 8080
```

`LAUNCHER_PUBLIC_HOST` matters: it is what appears in every link handed to
your team. Left as `localhost`, those links only work on the server itself.

Give the machine a **static IP or a DHCP reservation** before anyone
bookmarks anything — if the address moves, every saved link breaks at once.

### 5. Verify

```powershell
.venv\Scripts\python scripts\selftest.py
```

This builds a real FastAPI + Vite app, deploys it through the full pipeline,
and confirms that `GET /` serves the frontend and `GET /api/ping` reaches the
backend through the front server. It cleans up after itself and exits non-zero
on failure, so it also works as a smoke test after upgrades.

Failures name the fix rather than dumping a traceback (npm missing, `venv`
unavailable, no disk space, dependencies not installed).

```
--quick   skip the npm registry install (faster; does not test registry access)
--keep    leave the sample app running so you can open it in a browser
```

### 6. Start it at logon

Windows has no privileged-port restriction, so no elevation is needed to
serve on 8080 or 80. For it to come back after a restart, put a shortcut to
`start-launcher.ps1` in the Startup folder — press `Win+R`, run
`shell:startup`, and drop it there. No admin required.

Note the limitation: this starts when **someone logs in**. After an unattended
reboot the launcher stays down until a person signs in. Running it as a true
service that starts before logon does need administrator rights.

## Running with containers instead

If you ever get administrator access, or move this to a Linux machine, set
`LAUNCHER_RUNTIME=docker` and everything runs in containers instead — with
kernel-enforced memory and CPU limits, a private filesystem per app, and
Docker's own restart policy. The upload experience is identical.

On Windows that means enabling WSL (`wsl --install`, one elevated command),
installing Docker Engine inside it, and setting `networkingMode=mirrored` in
`.wslconfig` so ports inside WSL are reachable from the LAN. `deploy/` holds
the sample `.wslconfig`, firewall script and systemd unit for that path.

## What native mode does not give you

Worth knowing, because these are real:

- **Memory limits are soft.** The supervisor restarts an app that stays over
  `LAUNCHER_APP_MEMORY_MB` for several checks, but nothing stops it spiking
  hard between checks. Containers enforce this in the kernel.
- **No filesystem isolation.** Apps run as the same user and can read each
  other's files and data. Fine for a trusted internal team; not a boundary to
  rely on.
- **Node version conflicts are yours.** Python is isolated per app by its
  venv; npm is not.
- **WebSockets are not proxied.** Ordinary HTTP and streaming responses work.

## Configuration

All settings are environment variables (see `launcher/config.py`):

| Variable | Default | Notes |
|---|---|---|
| `LAUNCHER_PUBLIC_HOST` | `localhost` | **Set this.** Appears in every link handed out |
| `LAUNCHER_DATA_DIR` | `%LOCALAPPDATA%\AppLauncher` | ZIPs, sources, logs, database |
| `LAUNCHER_RUNTIME` | `native` | `native` or `docker` |
| `LAUNCHER_NPM` | auto-detected | Full path to `npm.cmd` if it is not on PATH |
| `LAUNCHER_PORT_START` / `_END` | `20000` / `29999` | Public ports for apps |
| `LAUNCHER_BACKEND_PORT_START` / `_END` | `30000` / `39999` | Loopback-only backend ports |
| `LAUNCHER_MAX_BUILDS` | `2` | Concurrent builds. Each needs ~2GB |
| `LAUNCHER_APP_MEMORY_MB` | `1024` | Soft per-app memory limit |
| `LAUNCHER_KEEP_VERSIONS` | `5` | Uploads retained per app |

## What the uploader has to get right

Almost nothing — but the ZIP has to contain a recognisable app. The dashboard
shows a prompt block to paste into Claude or Codex before asking for the ZIP,
which makes the output conform on the first try.

Auto-detection handles the common layouts. For anything unusual, a
`launcher.yaml` at the root wins:

```yaml
backend:
  path: ./api
  start: uvicorn main:app --host 0.0.0.0 --port 8000
frontend:
  path: ./web
  build: npm run build
  output: dist
```

Write the start command against port 8000; the launcher rewrites it to the
port it actually assigned. `$PORT` also works.

## Failures are handled as text, not as debugging

When a deploy fails the dashboard shows one plain sentence, and a **Copy error
for AI** button that puts a complete repair prompt on the clipboard — the
error, the log tail, and the layout rules. The uploader pastes it into their
AI assistant, gets a corrected ZIP, and re-uploads. They never diagnose
anything.

Things caught automatically, before or during the build:

- `node_modules`, `.venv`, `__pycache__` bundled into the ZIP (stripped)
- a wrapper folder around the project (unwrapped)
- **frontend hardcoded to `http://localhost:8000`** (caught before the build,
  because it is the most common failure and the slowest to discover)
- Python packages that do not exist on PyPI
- missing `build` script in `package.json`
- the app building but never answering HTTP

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/ -q
LAUNCHER_DATA_DIR=./data .venv/bin/uvicorn launcher.app:app --reload --port 8080
```

The test suite needs neither Docker nor a running server.
