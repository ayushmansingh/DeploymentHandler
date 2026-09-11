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

The front page is the directory of everything running on the server. It opens
with the machine's own numbers - memory, CPU and disk, each against its limit -
then one card per app, running ones first, showing its address, who deployed
it, and its current CPU, memory and disk use. **Clicking a card opens that application**;
the Manage button beside it goes to the app's own page for logs, replacing,
rollback and stop or start. The grid refreshes itself, so an app that is
mid-build turns into a working link without anyone reloading the page.

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
user only** (uncheck "Install for all users") — no administrator rights
needed.

**Prefer python.org over the Microsoft Store build.** Store Python is a
packaged app: Windows silently redirects its writes under `%LOCALAPPDATA%`
into a private per-package folder, so a virtual environment created there
ends up somewhere other than where it is looked for, and every deploy fails
with `failed to locate pyvenv.cfg`. Store Python does work as long as
`LAUNCHER_DATA_DIR` is outside `%LOCALAPPDATA%` — which is the default — and
the self-test refuses to run if that combination is wrong.

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
```

Then run `deploy\install-windows.cmd` (double-clicking works). It creates the
Python environment, installs dependencies, and runs the self-test.

### 4. Configure and start

Edit `deploy\settings.cmd` and set `LAUNCHER_PUBLIC_HOST` to the server's
own LAN address — find it with `ipconfig`. Left as an address the rest of the
network cannot reach, every link handed to your team will be broken.

Both scripts read that one file, so the self-test always checks the same
configuration the launcher actually runs with.

Give the machine a **static IP or a DHCP reservation** before anyone
bookmarks anything; if the address moves, every saved link breaks at once.

Run `deploy\start-launcher.cmd` to start it.

### 5. Verify

`install-windows.cmd` runs this for you, but you can run it again any time:

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
serve on 8080. For the launcher to come back after a restart, put a shortcut
to `deploy\start-launcher.cmd` in the Startup folder — press `Win+R`, run
`shell:startup`, and drop the shortcut there. No admin required.

A `.cmd` file is used rather than PowerShell because PowerShell's default
execution policy blocks unsigned `.ps1` scripts, which would be one more
thing to work around.

**This is the weakest point in the whole setup, so be clear-eyed about it.**
A Startup-folder shortcut runs when *someone logs in*, not when the machine
boots. After an unattended restart — a Windows update at 3am, a power cut —
nothing is running until a person signs in to that machine.

Apps themselves do not survive a reboot either; no process does. What survives
is on disk: the source, the built frontend, each app's environment, its port,
and its saved files. Once the launcher starts it re-launches everything that
was live, in seconds, with no rebuilding. But that only happens after the
launcher starts.

Running as a true service that starts before logon needs administrator
rights. Without them the practical options are:

- Log in to the server after any restart. Windows restarts are usually
  planned, so this is a known checkpoint rather than a surprise.
- Set Active Hours under Settings → Windows Update so updates do not restart
  the machine during the working day.
- Ask for one elevated session to create a scheduled task that runs at
  startup. It is a smaller ask than most, and it removes this problem
  permanently.

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

## Updating the launcher from another machine

Open `http://<server>:8080/admin/update` from any machine on the network and
upload the launcher's own ZIP. There is no need to go to the server.

**Your applications keep running throughout.** They are separate processes,
detached from the launcher, so replacing and restarting the launcher does not
touch them.

What happens, in order:

1. The ZIP is unpacked somewhere harmless and checked: it must contain the
   launcher's own files, and its Python must compile. Anything that fails here
   is rejected before a single file is replaced.
2. The current install is copied aside.
3. The new files are copied over it. `deploy\settings.cmd` and the Python
   environment are **never** replaced, so this server's settings survive.
4. Dependencies are installed if `requirements.txt` changed.
5. The launcher exits and a helper starts the new version.
6. The helper waits for it to answer. **If it does not, the copy from step 2 is
   restored and started instead** - so a bad upload costs about ten seconds
   rather than a trip to the machine.

The page you upload from waits for the launcher to come back and then reloads
itself. The last update's log is on the same page, and the new launcher's own
startup output is kept at `<data>/updates/launcher-start.log` - which is where
to look if an update ever does fail.

## Settings an app needs

An app that needs an API key or a token reads it from the environment:

```python
KEY = os.environ.get("REDASH_API_KEY", "")
```

The values are set from the app's own page in the dashboard - name and value,
both typed in, so each app uses whatever names its code expects. They are
handed to the app when it starts, survive being replaced with a newer ZIP, and
never travel inside the ZIP itself.

An app declares what it needs in `launcher.yaml`, names only:

```yaml
settings:
  - name: REDASH_API_KEY
    description: Personal API key from Redash, under Profile
```

**An app that declares settings is not started until they are set.** It builds
as normal and then waits, showing on the dashboard as needing setting up with
the names it is waiting for. Entering the last one starts it. So an app is
never running in a half-configured state, and nothing has to be restarted
afterwards.

On a replace, the version already running carries on serving until the new one
is configured - a working app does not go down because its replacement needs a
key.

**Values cannot be read back.** Once saved, the page shows the name, a mask,
and when it last changed. Changing one means typing a new value over it.
Changing a value restarts the app; removing one it declared stops it until it
is set again.

Settings can never override `PORT`, `APP_DATA_DIR` or `PYTHONUNBUFFERED` - the
launcher applies its own last, so a stored value cannot break an app's port or
point it away from its saved files.

This is not a secret store, and it should not be treated as one. The values
sit in plaintext in the launcher's database, and any app deployed here can
read its own environment. It keeps a token from being read off the screen by
the next person to open the page. Anything you would not put in a shared team
folder does not belong here.

## Saved files

Every deploy replaces an app's source folder, so anything written beside the
code would be lost on the next upload. Each app therefore gets a directory
that no deploy touches:

```
<LAUNCHER_DATA_DIR>/appdata/<app-name>/
```

An app reaches it two ways. `APP_DATA_DIR` is set in its environment and is
the reliable route. As a convenience the same directory is linked in at
`data/` next to `main.py`, so ordinary code like `pd.read_csv("data/export.csv")`
works too — which matters, because that is what an AI assistant tends to write.

This is what makes the common shape of work possible: pull a query result out
of Redash into a CSV, build a dashboard on it, then keep updating the
dashboard without losing the data.

A `data/` folder shipped inside the ZIP is treated as seed data — copied in on
the first deploy, and never allowed to overwrite what the running app has
since collected. Deleting an app deletes its data; nothing else does.

On Windows the link is a directory junction, which needs no administrator
rights. If it cannot be created the deploy still succeeds and `APP_DATA_DIR`
still works — only the `data/` shorthand is unavailable, and the log says so.

## Configuration

All settings are environment variables (see `launcher/config.py`):

| Variable | Default | Notes |
|---|---|---|
| `LAUNCHER_PUBLIC_HOST` | `localhost` | **Set this.** Appears in every link handed out |
| `LAUNCHER_DATA_DIR` | `%USERPROFILE%\AppLauncherData` | ZIPs, sources, logs, saved app data. **Never put this under `%LOCALAPPDATA%`** |
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

The block lives in [PROMPT.md](PROMPT.md), which is the canonical copy — the
dashboard renders the same text, and a test fails if the two drift apart.

When an upload fails and the plain-English message is not enough,
[PIPELINE.md](PIPELINE.md) describes exactly what the server does to a ZIP —
every check, every rewrite, every path. It is written to be pasted into an AI
assistant alongside the error.

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

## Proposals not yet built

- [Agent access](docs/agent-access.md) - letting an AI agent discover the
  server, read the rules, deploy and follow its own build, without a person
  driving the browser.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/ -q
LAUNCHER_DATA_DIR=./data .venv/bin/uvicorn launcher.app:app --reload --port 8080
```

The test suite needs neither Docker nor a running server.
