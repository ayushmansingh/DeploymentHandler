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
- Read every setting - API keys, tokens, URLs - from the environment:
      KEY = os.environ.get("REDASH_API_KEY", "")
  Do NOT read a .env file from the project folder; nobody is at the server to
  create one. See SETTINGS below for how they get there
- The server starts the app for you. A `if __name__ == "__main__"` block is
  harmless but is not used, and the port is chosen by the server

LAUNCHER.YAML - ONE FILE AT THE ROOT OF THE ZIP
- It is OPTIONAL. An app that is just backend/ and frontend/ needs no
  launcher.yaml at all - the server finds those folders on its own. Write one
  only to declare settings
- If you write it, write the WHOLE file. A launcher.yaml containing only
  `settings:` is the single most common way a deploy fails
- It reads exactly TWO structural keys, `backend:` and `frontend:`, and each
  is a BLOCK with `path:` underneath - not a string, not a list. No other
  name works: api, server, service, app, web, client and ui are all ignored.
  This is the entire file:

      backend:
        path: backend
      frontend:
        path: frontend
        build: npm run build
        output: dist
      settings:
        - name: REDASH_API_KEY
          description: Personal API key from Redash, under Profile
        - name: SLACK_WEBHOOK
          description: Optional - alerts are sent here if it is set
          required: false
        - name: PAGE_SIZE
          description: Rows per page
          default: 50

SETTINGS THE APP NEEDS
- Settings the app needs a PERSON to supply go under `settings:` in that
  file. Do not ship a .env file for these
- The server sets three variables itself and passes them in. Read them with
  os.environ exactly as normal, but NEVER list them under `settings:`:
      PORT              the port to listen on
      APP_DATA_DIR      where to write anything worth keeping
      PYTHONUNBUFFERED  so your logs appear straight away
  Declaring one of those is the most common launcher.yaml mistake, because
  the app really does read it - but it is the server's to provide, not
  yours to ask for
- Three kinds, and the difference matters:
    SECRET - a key, token or password. Name it, never write the value. A
      person types it into the server and the app starts once they have
    OPTIONAL - the app runs without it. Add `required: false`, or it will
      hold the app back
    APP'S OWN - a value the app decides, like a page size, a log level or a
      timezone. Give it a `default:` and nobody is asked for anything; it can
      still be changed on the server later
- A default is shown in full on the dashboard because it travels inside the
  ZIP. NEVER give a default to a key, token or password
- The app does not start until every setting that is required, and has no
  default, has been given a value

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

**`launcher.yaml` is optional, and its shape is exact.** The launcher reads
two structural keys, `backend:` and `frontend:`, each a block with `path:`
under it. Anything else - a string instead of a block, a list, or keys called
`api:`, `server:` or `web:` - describes nothing, and the settings alone
describe nothing either. That last case was the common one, because the
prompt used to show `settings:` on its own and an AI reasonably copied just
that. A manifest that names neither now falls back to reading the folders
rather than failing, so these deploys succeed with a note in the log - but
the file is still worth writing correctly, and worth leaving out entirely
when there are no settings to declare.

**Settings declared in `launcher.yaml`, valued on the server.** Nobody is at
a terminal on the server to create a `.env`. The ZIP says which settings the
app needs; a person types the values into the dashboard. The launcher then
holds the app - built, not started - until they are all set, so it never runs
in a half-configured state and never needs restarting afterwards.

Declaring them is what makes that work. An app that reads
`os.environ["REDASH_API_KEY"]` without declaring it starts anyway, fails on
the missing key, and looks like a crash.

`required: false` is the other half. A setting the app can run without - an
alert webhook, a feature nobody has turned on yet - is still worth declaring
so it appears on the app's page and nobody has to read the source to discover
it exists. It just does not hold the app back.

**`default:` is for the settings the app leads with rather than asks for.** A
page size, a log level, a timezone: things that belong in configuration but
that nobody should be made to type before the app will run. The launcher puts
the default in the environment, so the app starts unattended, and the value is
still listed on the page where anyone can change it later without editing and
re-uploading the ZIP. An override can be reset, which puts the default back.

This is where a `.env` file's non-secret half belongs. A default is visible on
the dashboard - it shipped inside the ZIP, so pretending otherwise would only
stop people checking what the app is running with. That makes the rule simple:
if it has a default, it is not a secret. Keys, tokens and passwords are
declared with no default and typed in on the server.

One thing `launcher.yaml` cannot cover is a frontend build variable - Vite
reads `VITE_*` while `npm run build` runs, which is long before anyone is
asked for a value. Those stay in a `.env` file inside the ZIP, and they are
baked into the JavaScript the browser downloads, so nothing secret goes there
either.

**Module-level `app`.** That is what the launcher looks for to work out how to
start the backend.

**`APP_DATA_DIR` for anything worth keeping.** Each deploy replaces the app's
source folder, so a file written beside the code would be lost on the next
upload. `APP_DATA_DIR` points outside that folder and is never touched by a
deploy, so a CSV pulled from Redash, a SQLite database or a cache all survive
being replaced. Deleting the app deletes its data; nothing else does.
