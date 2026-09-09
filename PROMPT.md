# The prompt block

Paste this into Claude or Codex **before** asking for the ZIP. It makes the
generated project conform to what the launcher expects, which is worth more
than any amount of validation on our side.

This file is the canonical copy; the dashboard shows the same text with a
Copy button.

```text
Package this as a ZIP for our internal app server. Follow these rules exactly.

STRUCTURE
- Two folders at the root of the ZIP: backend/ and frontend/
- Do NOT include node_modules, .venv, or build output in the ZIP

BACKEND (Python)
- backend/main.py must create the app at module level:  app = FastAPI()
- backend/requirements.txt, pinned to versions that really exist on PyPI
- EVERY backend route must start with /api
  e.g. @app.get("/api/items") - a route at /items will not be reachable
- The app must start with no .env file present; use safe defaults for every
  setting, and never require an API key to boot
- The server starts the app for you. A `if __name__ == "__main__"` block is
  harmless but is not used, and the port is chosen by the server

SAVING FILES
- To save anything - a CSV pulled from Redash, a SQLite database, a cache -
  write it inside the folder given by the APP_DATA_DIR environment variable:
      DATA = Path(os.environ.get("APP_DATA_DIR", "data"))
      DATA.mkdir(parents=True, exist_ok=True)
      df.to_csv(DATA / "redash_export.csv", index=False)
- Files there survive when the app is replaced with a newer ZIP
- The folder `data/` next to main.py points at the same place, so a plain
  "data/report.csv" also works
- Anything written anywhere ELSE is erased on the next upload

FRONTEND (React + Vite)
- frontend/package.json must have a "build" script
- The build must output to frontend/dist
- EVERY API call must use a relative path starting with /api
  e.g. fetch("/api/items")  -  NEVER http://localhost:8000 or any absolute URL

NOT SUPPORTED - do not use these
- WebSockets
```

## Why each rule is there

**Routes must start with `/api`.** The app is served from one address: `/`
returns the frontend, and only `/api/...` is forwarded to the Python backend.
A route at `/items` is unreachable no matter how correct the code is.

**Relative API calls.** `http://localhost:8000` means the machine the browser
is running on, not the server. This is the single most common failure, so the
launcher rejects it before building rather than after.

**No required `.env`.** Nobody is at a terminal to create one, and a backend
that exits on a missing key looks identical to a crash.

**Module-level `app`.** That is what the launcher looks for to work out how to
start the backend.

**`APP_DATA_DIR` for anything worth keeping.** Each deploy replaces the app's
source folder, so a file written beside the code would be lost on the next
upload. `APP_DATA_DIR` points outside that folder and is never touched by a
deploy, so a CSV pulled from Redash, a SQLite database or a cache all survive
being replaced. Deleting the app deletes its data; nothing else does.
