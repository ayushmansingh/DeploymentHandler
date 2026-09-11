# What the server does to your ZIP

Paste this into Claude or Codex when an upload fails. It describes exactly
what happens between clicking Deploy and the app being live, so the assistant
can reason about where a failure came from instead of guessing.

Everything below is what the launcher actually does, in order.

---

## 1. Upload

The ZIP is streamed to disk and kept forever (the newest 5 per app), so any
version can be restored later. Rejected here:

- larger than 200 MB
- not a valid ZIP file

## 2. Unpack

The previous version's folder is deleted, then the ZIP is unpacked in its
place. **If the running app has a file open inside that folder — a SQLite
database is the usual case — Windows will not let it be deleted. The launcher
stops the app to release the handles and continues.** The app is briefly down
in that case, and the log says so.

While unpacking:

- **These folders are silently dropped**, wherever they appear:
  `node_modules`, `.venv`, `venv`, `env`, `__pycache__`, `.git`, `.next`,
  `dist`, `build`, `.pytest_cache`, `.mypy_cache`, `.DS_Store`, `.idea`,
  `.vscode`, `coverage`, `.tox`, `.cache`
  Note `dist` and `build` are dropped: the frontend is always rebuilt on the
  server, so shipping a build is pointless, not harmful.
- **A single wrapper folder is unwrapped.** A ZIP containing one folder
  containing the project becomes just the project.
- Rejected: paths escaping the folder (`../`), absolute paths, symlinks,
  more than 20,000 files, more than 1 GB unpacked.

## 3. Work out what the project is

If `launcher.yaml` exists at the root it wins. Otherwise:

**Backend** — the first directory containing `requirements.txt`, preferring
`backend/`, `api/`, `server/`, `app/`, `src/`, then any directory up to two
levels deep. Falls back to `pyproject.toml`, then to any `.py` file.

**How it starts the backend** is read from the code, looking at `main.py`,
`app.py`, `server.py`, `api.py`, `run.py` in that order:

| Found in the file | Command used |
|---|---|
| `app = FastAPI(` at module level | `uvicorn main:app` |
| `app = Flask(` at module level | uvicorn in WSGI mode (gunicorn does not run on Windows) |
| `manage.py` exists | `python manage.py runserver` |
| `__main__` block only | `python main.py` |

The variable name is read from the code, so `api = FastAPI()` works too. **The
assignment must be at module level** — created inside a function, it is not
found, and detection fails with "could not tell how to start it".

**Frontend** — the first directory containing `package.json`, preferring
`frontend/`, `web/`, `client/`, `ui/`, `app/`. The build output directory is
inferred: `dist` for Vite, `build` for create-react-app, `out` for Next.
`package.json` **must** have a `build` script.

## 4. Preflight, before anything is built

Every `.js .jsx .ts .tsx .vue .svelte .env` file under the frontend folder is
scanned for `http://localhost` or `http://127.0.0.1`. If one is found the
deploy **fails immediately**, before the two-minute build, because a frontend
pointing at localhost is dead for everyone but its author. Framework config
files (`vite.config.*`, `next.config.*`, and similar) are skipped, since a
dev-server proxy pointing at localhost is correct.

## 5. Saved files are attached

The app gets a permanent directory outside its source folder:

```
<data root>/appdata/<app-name>/
```

- It is passed to the backend as the **`APP_DATA_DIR`** environment variable.
- It is also linked in at **`data/` next to the backend's main file**, so a
  relative path like `data/app.db` reaches the same place.
- A `data/` folder inside the ZIP is treated as seed data: copied in once, and
  never allowed to overwrite files the running app has already written.

**Anything written anywhere else is destroyed on the next upload**, because
step 2 deletes the whole source folder.

## 6. Build

**Backend**, if there is one:

1. `python -m venv` — a private environment for this app only
2. `pip install -r requirements.txt`
3. `pip install uvicorn` — a safety net, since generated projects often omit
   the server they are started with

**Frontend**, if there is one:

1. `npm ci` in the frontend folder, falling back to `npm install`
2. `npm run build`
3. The build output directory must exist afterwards, or the deploy fails

Both steps time out after 15 minutes. Two apps build at once at most.

## 7. Start

- The backend runs with its **working directory set to the backend folder**,
  so relative paths resolve from there — `data/app.db` means
  `<backend folder>/data/app.db`.
- It is bound to **127.0.0.1 on a private port**, not to the network. The port
  is assigned by the server; whatever port the code asks for is rewritten.
- Environment: `APP_DATA_DIR`, `PORT`, `PYTHONUNBUFFERED=1`, the server's own
  environment, and **any settings configured for this app** on its page. Those
  are applied first, so they can never displace `PORT` or `APP_DATA_DIR`.
- A front server takes the app's public port and serves:
  - `/` → the built frontend files
  - `/api/...` → proxied to the backend
  - any unmatched path → `index.html`, so single-page routing survives a refresh

**Only `/api/...` reaches the backend.** A route at `/items` is unreachable.

## 8. Verify

The server polls the app's public address for up to 45 seconds. Any HTTP
response counts as alive. No response in 45 seconds means the deploy is marked
failed and the app's own output is added to the log.

## 9. After it is live

- A supervisor checks every 15 seconds that the processes are alive, and
  restarts the app if they are not.
- An app over 1 GB of memory for 3 consecutive checks is restarted. Its
  current CPU, memory and disk use are shown on the dashboard.
- Apps are started again after the launcher restarts, and after a machine
  restart once the launcher itself is running again. Nothing is rebuilt: the
  environment and the built frontend are already on disk.
- A build interrupted by the server stopping is marked failed on the next
  start, rather than being left as "building" forever.
- The launcher itself can be replaced from `/admin/update` without going to
  the server. Applications keep running while it restarts.

---

## Things that fail, and what they look like

| Symptom in the log | Cause |
|---|---|
| `No matching distribution found for X` | package name or version does not exist on PyPI |
| `ModuleNotFoundError: No module named 'X'` | imported but missing from `requirements.txt` |
| `npm ERR! missing script: build` | no `build` script in `package.json` |
| `npm ERR! code ERESOLVE` | incompatible frontend dependency versions |
| "trying to reach http://localhost:8000" | absolute API URL in the frontend; use `/api/...` |
| "could not tell how to start it" | no module-level `app = FastAPI()` found |
| "built successfully but did not start" | backend crashed on startup — its own output follows in the log |
| "previous version has files open" | the running app held a file inside its source folder; it was stopped and the deploy continued |
| App runs but the page cannot reach the API | backend route is not under `/api` |
| Data disappears after an update | written outside `APP_DATA_DIR` |

## Rules that follow from all of this

1. Backend routes start with `/api`. Frontend calls use relative `/api/...`.
2. `app = FastAPI()` at module level in `main.py`.
3. Everything saved goes under `APP_DATA_DIR` (or `data/`, which is the same
   place). Nothing else survives an update.
4. Settings come from the environment, set on the app's page. The app must
   boot without them - it can be deployed first and configured afterwards.
5. Do not pin package versions that do not exist. Do not ship `node_modules`.
6. Do not choose a port; the server assigns one.
7. WebSockets are not proxied.
