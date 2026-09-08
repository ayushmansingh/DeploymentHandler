# App Launcher

Drop a ZIP, get a running app on the office network.

Built for a team that writes dashboards with an AI assistant, downloads the
ZIP, and wants it running somewhere everyone can see — without learning Git,
Docker, or the command line.

## What your team does

1. Open `http://<server>/` on the LAN.
2. Type a name, choose the ZIP from their Downloads folder, click **Deploy**.
3. Watch the build log scroll.
4. Get a link like `http://192.168.1.50:24817`.

Re-uploading under the same name updates the app and **keeps the same link**.
Every upload is retained, so any previous version can be restored with one
click.

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

Nobody on the team ever sees Docker, a terminal, or a port number they have to
manage.

## How it works

Each app becomes exactly one container, laid out the same way:

```
   nginx :80          <- the only published port
     |-- /            static frontend build   (if the app has one)
     '-- /api  ---->  127.0.0.1:8000          (the Python backend, if any)
```

Serving both halves from one origin is what makes relative `/api/...` calls
work with no per-app configuration. Every container uses the same internal
ports because each has its own network namespace; only the *host* port is
unique, and it is recorded in a SQLite registry so it never changes under an
app once assigned.

The pipeline:

```
upload -> extract -> detect -> generate Dockerfile -> preflight
       -> build -> allocate port -> run -> verify HTTP -> live
```

## Setting up the server (Windows host)

The launcher runs Linux containers, so on Windows it lives inside WSL2. Use
**Docker Engine inside WSL**, not Docker Desktop — same result, no licensing
question for a company.

### 1. Install WSL2 with Ubuntu

Check the Windows version first with `winver`. Mirrored networking needs
Windows 11 22H2 (build 22621) or newer; without it, ports inside WSL are not
reachable from the LAN, which defeats the point. In an elevated PowerShell:

```powershell
wsl --update
wsl --install -d Ubuntu-24.04
```

`wsl --update` matters even on a fresh machine: mirrored networking needs a
recent WSL release, which updates independently of Windows itself.

### 2. Configure WSL resources and networking

Copy `deploy/wslconfig.example` to `C:\Users\<you>\.wslconfig`, adjust the
memory line, then `wsl --shutdown` and reopen Ubuntu.

`networkingMode=mirrored` is the setting that makes this work: WSL shares the
Windows network stack, so a port bound inside WSL is reachable from other
machines on the network with no port forwarding.

Two caveats worth knowing before you rely on it:

- Mirrored mode can conflict with some corporate VPN clients and with other
  hypervisors (VirtualBox, VMware). If the network misbehaves after enabling
  it, that is the first thing to suspect.
- Inbound traffic to WSL still passes through the Windows Firewall, so the
  rules in step 5 are required, not optional.

Confirm it took effect — this should print the Windows LAN IP, not a
172.x.x.x address:

```bash
ip addr show eth0 | grep 'inet '
```

### 3. Install Docker inside WSL

```bash
sudo apt update && sudo apt install -y docker.io python3-venv python3-pip
sudo usermod -aG docker $USER   # log out and back in for this to apply
```

The launcher runs as a systemd service, so check systemd is enabled inside
WSL — Ubuntu 24.04 enables it by default, but confirm:

```bash
cat /etc/wsl.conf     # expect [boot] with systemd=true
systemctl is-system-running   # "running" or "degraded" are both fine
```

If it is missing, add it and run `wsl --shutdown` from PowerShell:

```ini
[boot]
systemd=true
```

Then:

```bash
sudo systemctl enable --now docker
```

### 4. Install the launcher

```bash
sudo git clone <this repo> /opt/applauncher
cd /opt/applauncher
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
```

Edit `deploy/applauncher.service` and set `LAUNCHER_PUBLIC_HOST` to the
server's LAN IP, then:

```bash
sudo cp deploy/applauncher.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now applauncher
```

### 5. Open the firewall and start WSL on boot

Run `deploy/windows-firewall.ps1` in an elevated PowerShell.

WSL does not start by itself at boot. Create a Task Scheduler task that runs
at system startup, as SYSTEM, with:

```
wsl.exe -d Ubuntu-24.04 -u root /bin/true
```

That boots the distro; systemd then starts Docker and the launcher.

### 6. Verify the install

```bash
sudo /opt/applauncher/.venv/bin/python /opt/applauncher/scripts/selftest.py
```

This builds a real FastAPI + Vite app, deploys it through the full pipeline,
and confirms that `GET /` serves the frontend and `GET /api/ping` reaches the
backend through nginx — the two things that prove the whole chain works. It
cleans up after itself and exits non-zero on failure, so it is also usable as
a smoke test after upgrades.

Failures name the fix rather than dumping a traceback (`docker` not running,
user not in the docker group, no disk space, dependencies missing).

```
--quick   skip the npm registry install (faster; does not test registry access)
--keep    leave the sample app running so you can open it in a browser
```

### If mirrored networking is unavailable

On Windows 10, or an older Windows 11, WSL sits behind NAT and its ports are
not reachable from the LAN. Options, best first: upgrade to Windows 11 22H2+;
install Ubuntu Server on the machine directly (which also reclaims the RAM
Windows is using); or maintain `netsh interface portproxy` rules per app port,
re-applied whenever the WSL IP changes — workable but fragile, since this
launcher allocates ports dynamically.

## Configuration

All settings are environment variables (see `launcher/config.py`):

| Variable | Default | Notes |
|---|---|---|
| `LAUNCHER_PUBLIC_HOST` | `localhost` | **Set this.** Appears in every link handed out |
| `LAUNCHER_DATA_DIR` | `/var/lib/applauncher` | ZIPs, sources, logs, database |
| `LAUNCHER_PORT_START` / `_END` | `20000` / `29999` | Host port range for apps |
| `LAUNCHER_MAX_BUILDS` | `2` | Concurrent builds. Each needs ~2GB |
| `LAUNCHER_APP_MEMORY` | `1g` | Per-app memory cap |
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
  because it is the single most common failure and the slowest to discover)
- Python packages that do not exist on PyPI
- missing `build` script in `package.json`
- the app building but never answering HTTP

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/ -q
LAUNCHER_DATA_DIR=./data .venv/bin/uvicorn launcher.app:app --reload --port 8000
```

The test suite covers archive safety, detection, port allocation and error
translation, and needs no Docker daemon.
