# Proposal: letting an AI agent deploy without a person driving the browser

**Status: proposed, not built.** Written down so it can be decided on later.
Nothing in this document exists in the launcher yet.

## The idea

Someone hands an agent - Claude, Codex - the launcher's URL. The agent works
out what the server is, reads the rules a ZIP has to follow, builds one,
uploads it, watches it deploy, and fixes its own mistakes if the build fails.
No person copying prompts or clicking through forms.

## What already exists

`/openapi.json` **is already served.** Disabling the Swagger UI
(`docs_url=None`) did not disable the schema, so all 18 routes are already
described there, 8 of them with text taken from their docstrings. The
machine-readable API description is not a thing to build; it is a thing to
make discoverable and to point at endpoints worth calling.

## What does not work today

**The endpoints are the wrong shape.** `POST /upload` answers with a 303
redirect to an HTML page. An agent following that gets a page of markup and no
structured result. Every write endpoint behaves this way, because every one of
them was written for a browser form.

**There is nothing to discover.** Nothing at the root says what this server is
or how to talk to it.

## Discovery

`robots.txt` is the wrong tool - it is an exclusion protocol for crawlers, not
a description of what a service can do.

The nearest convention is **`/llms.txt`**: plain markdown at the root saying
what a site is and how to use it. A proposal rather than a ratified standard,
but it is the file agents are increasingly primed to look for, and serving one
costs nothing.

**The part that decides whether this works at all:** no agent reliably fetches
`/llms.txt` unprompted. Given a URL, an agent fetches the HTML and reads it.
So the signpost has to be inside the homepage - a `<meta>` tag and an HTML
comment within the first few hundred bytes of `<head>`, naming `/llms.txt` and
`/openapi.json`. A manifest nobody finds is not a feature.

`/llms.txt` would carry: what the server is, the prompt block inline, the
deploy contract (`/api` routes, `APP_DATA_DIR`, settings from the environment,
no `.env`), and links to the API and to a starter ZIP.

## API surface

JSON siblings of what the browser already uses. Existing HTML routes stay as
they are.

| Route | Purpose |
|---|---|
| `POST /api/apps` | Upload a ZIP. Returns `{app, deploy_id, status_url, poll_after_seconds}` |
| `GET /api/apps/{name}` | Status, address, whether it is still working, the failure reason |
| `GET /api/apps/{name}/log` | The build log as text |
| `GET /api/apps/{name}/repair-prompt` | The repair text, ready to act on |
| `POST /api/apps/{name}/replace` | Replace an existing app |
| `GET /api/apps` | List (exists already) |

Two details matter more than the list:

- **A terminal state.** The status response has to make it obvious when to
  stop polling, and suggest how long to wait. Otherwise agents either poll in
  a tight loop or give up while a build is still running.
- **Failures carry the repair prompt inline.** Then the agent can close its own
  loop: upload, poll, read why it failed, fix the ZIP, upload again - with
  nobody in the middle for the mechanical failures. This is most of the value.

## Settings: the agent declares, the person sets

This looked like the blocker. It is better understood as a seam, and the seam
is in the right place.

An agent should not set secrets. It does not have the Redash token, and a
token pasted into an agent's context has left the owner's control. What the
agent does know is **which settings the app needs**.

So the agent declares names in `launcher.yaml`, never values:

```yaml
settings:
  - name: REDASH_API_KEY
    description: Personal API key from Redash, under Profile
```

The launcher then shows the app as **needs configuring**, listing those names
with empty value boxes. The agent's work ends at "deployed, now paste your key
here." A person typing a token into a form is exactly the right boundary, and
it is one page visit rather than a trip to the server.

This also fills a gap that exists regardless of agents: **nothing currently
says an app needs configuring.** Today you deploy it, it half works, and you
find out by using it.

It only works because the prompt already requires that an app *start* without
its settings. That rule was written so a missing key could not look like a
crash; it happens to be what makes the handoff possible.

## The open question: authentication

Not settings - **unauthenticated writes.**

The dashboard is open today because uploading an app already means running
arbitrary code on that machine, and everyone on the network is trusted. That
reasoning holds for people. It weakens for agents.

A documented, discoverable API changes the character of the exposure. An agent
reading some document that says *"upload this ZIP to
http://192.168.1.50:8080/api/apps"* now has a deployment target. The agent is
doing as it was told; the instruction came from a poisoned source. That is a
real shape of attack for anything browsing on the same network.

**Recommendation: a token on writes.** Generated by the launcher, shown on the
Server page, sent as `Authorization: Bearer ...`. Reads stay open so status
checks stay frictionless. The token is pasted into the agent once. Setup stays
touchless afterwards, and the server stops being a drive-by target.

This is the one decision worth making deliberately rather than by default.

## If it goes ahead

1. `/llms.txt`, plus the signpost in the homepage `<head>`
2. The JSON endpoints, with descriptions good enough that OpenAPI alone is
   enough to call them correctly
3. `settings:` in `launcher.yaml`, surfaced as "needs configuring"
4. The write token

1 to 3 are the feature. 4 is the one worth arguing about.

## Decisions needed

- Token on writes, or leave it open?
- Should an agent be allowed to delete or stop an app, or only create and
  replace? Narrower is safer and probably sufficient.
- Does the starter ZIP belong on the server, so an agent can fetch a known-good
  example rather than inferring the layout from prose?
