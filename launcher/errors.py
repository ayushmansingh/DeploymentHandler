"""Turn build and runtime failures into something a non-technical uploader can
act on — and into a prompt they can paste straight back into their AI assistant.

The team here does not debug; they ferry text between the dashboard and Claude
or Codex. So every failure needs two things: one plain sentence saying what
went wrong, and a ready-made repair prompt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_LOG_TAIL_LINES = 40


@dataclass
class Diagnosis:
    summary: str          # one plain-English sentence
    detail: str | None    # the specific offending token, if we found one
    hint: str | None      # what to do about it


# Ordered most-specific first: the first pattern that matches wins.
_PATTERNS: list[tuple[str, str, str | None]] = [
    (
        r"(?:failed to connect to the docker API|Cannot connect to the Docker daemon)",
        "The server's container system is not running, so nothing can be deployed "
        "right now.",
        "This is a problem with the server, not with your ZIP. Please tell the "
        "server administrator.",
    ),
    (
        r"No matching distribution found for ([\w\-\[\].]+)",
        "One of your Python packages does not exist: {0}.",
        "AI assistants sometimes invent package names or versions. Ask for a "
        "corrected requirements.txt using packages that exist on PyPI.",
    ),
    (
        r"Could not find a version that satisfies the requirement ([\w\-\[\].]+)",
        "The version pinned for the Python package {0} does not exist.",
        "Ask your AI assistant to pin a real published version, or to remove "
        "the version pin entirely.",
    ),
    (
        r"ModuleNotFoundError: No module named '([\w\.]+)'",
        "Your backend imports {0}, but it is not listed in requirements.txt.",
        "Ask your AI assistant to add the missing package to requirements.txt.",
    ),
    (
        r"npm ERR! 404 +'?([\w@\-/.]+)'? is not in (?:this registry|the npm registry)",
        "One of your frontend packages does not exist: {0}.",
        "Ask your AI assistant to replace it with a real package from npm.",
    ),
    (
        r"npm ERR! missing script: ?\"?build\"?",
        "Your package.json has no \"build\" command.",
        "Ask your AI assistant to add a build script to package.json.",
    ),
    (
        r"npm ERR! code ERESOLVE",
        "Your frontend packages conflict with each other and cannot be installed together.",
        "Ask your AI assistant for a package.json with compatible versions.",
    ),
    (
        r"(?:SyntaxError|IndentationError): (.+)",
        "There is a syntax error in your Python code: {0}",
        "Paste the error into your AI assistant and ask for a corrected file.",
    ),
    (
        r"error TS\d+: (.+)",
        "TypeScript rejected your frontend code: {0}",
        "Ask your AI assistant to fix the type error and send a new ZIP.",
    ),
    (
        r"Address already in use",
        "Your backend tried to use a port that was already taken inside its container.",
        "Make sure only one server is started, listening on 0.0.0.0:8000.",
    ),
    (
        r"(?:Killed\b|signal 9|non-zero code: 137|exit code 137)",
        "The build ran out of memory on the server.",
        "This usually means a very large frontend build. Try again when the "
        "server is quieter, or ask for a smaller set of dependencies.",
    ),
]


def diagnose(log_text: str) -> Diagnosis:
    """Best-effort plain-English reading of a build or startup log."""
    for pattern, template, hint in _PATTERNS:
        match = re.search(pattern, log_text)
        if not match:
            continue
        groups = [g.strip() for g in match.groups()]
        return Diagnosis(
            summary=template.format(*groups) if groups else template,
            detail=groups[0] if groups else None,
            hint=hint,
        )

    return Diagnosis(
        summary="The build failed. The last few lines of the log are below.",
        detail=None,
        hint="Use the \"Copy error for AI\" button and paste it into your AI "
        "assistant to get a corrected ZIP.",
    )


def check_localhost_leak(bundle_text: str) -> Diagnosis | None:
    """Detect a frontend built to call the developer's own machine.

    This is the single most common failure for AI-generated dashboards: the
    code works on the author's laptop and is dead everywhere else.
    """
    match = re.search(r"https?://(?:localhost|127\.0\.0\.1)(?::(\d+))?", bundle_text)
    if not match:
        return None
    port = match.group(1) or "80"
    return Diagnosis(
        summary=f"Your dashboard is trying to reach http://localhost:{port}, "
        "which only exists on the computer it was written on.",
        detail=match.group(0),
        hint="All API calls need to be relative paths starting with /api "
        "(for example fetch(\"/api/items\")). Use the \"Copy error for AI\" "
        "button and ask for a corrected ZIP.",
    )


def log_tail(log_text: str, lines: int = MAX_LOG_TAIL_LINES) -> str:
    """The last meaningful lines of a log, with blank noise removed."""
    kept = [ln.rstrip() for ln in log_text.splitlines() if ln.strip()]
    return "\n".join(kept[-lines:])


def repair_prompt(app_name: str, diagnosis: Diagnosis, log_text: str) -> str:
    """A complete prompt the uploader can paste into Claude or Codex."""
    parts = [
        f'My app "{app_name}" failed to deploy on our internal app server.',
        "",
        f"What went wrong: {diagnosis.summary}",
    ]
    if diagnosis.hint:
        parts += ["", f"The server suggests: {diagnosis.hint}"]
    parts += [
        "",
        "Here are the last lines of the build log:",
        "",
        log_tail(log_text),
        "",
        "Please fix this and give me a corrected ZIP. Keep these requirements:",
        "- Layout: backend/ (Python) and frontend/ (React + Vite)",
        "- backend/requirements.txt pinned to versions that exist on PyPI",
        "- Backend listens on 0.0.0.0:8000",
        "- frontend/package.json must have a \"build\" script",
        "- ALL frontend API calls use relative paths starting with /api",
        "  (for example fetch(\"/api/items\") - never http://localhost:8000)",
        "- The app must start with no .env file present; use safe defaults",
        "- Do not include node_modules or .venv in the ZIP",
    ]
    return "\n".join(parts)
