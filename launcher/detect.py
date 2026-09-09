"""Work out what an extracted project actually is.

Order of preference:
  1. launcher.yaml, if the uploader supplied one (the escape hatch)
  2. convention-based detection (the path AI-generated projects almost always
     land on: backend/ + frontend/, requirements.txt, package.json)

Detection returns a Spec; anything it cannot work out is reported through
DetectionError with a message aimed at a non-technical uploader.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# Directory names we check, in priority order, when locating each half.
BACKEND_HINTS = ("backend", "api", "server", "app", "src")
FRONTEND_HINTS = ("frontend", "web", "client", "ui", "app")

# Where each frontend toolchain writes its production build.
BUILD_OUTPUT_BY_TOOL = {"vite": "dist", "next": "out", "cra": "build"}


class DetectionError(Exception):
    """The project layout could not be understood."""


@dataclass
class BackendSpec:
    path: str                 # relative to project root
    start: str                # the command to run inside the container
    requirements: str | None  # relative path to requirements.txt, if any


@dataclass
class FrontendSpec:
    path: str
    build: str
    output: str


@dataclass
class Spec:
    kind: str                       # backend | frontend | fullstack
    backend: BackendSpec | None
    frontend: FrontendSpec | None
    notes: list[str]


def _find_dir_containing(root: Path, filename: str, hints: tuple[str, ...]) -> Path | None:
    """Locate the directory holding `filename`, preferring conventional names.

    Searches the root, then hinted subdirectories, then any directory at most
    two levels deep — enough for real layouts without walking node_modules-
    sized trees (which are stripped before we get here anyway).
    """
    if (root / filename).is_file():
        return root
    for hint in hints:
        if (root / hint / filename).is_file():
            return root / hint
    candidates = sorted(
        (p.parent for p in root.glob(f"*/{filename}") if p.is_file()),
        key=lambda p: p.name,
    )
    candidates += sorted(
        (p.parent for p in root.glob(f"*/*/{filename}") if p.is_file()),
        key=lambda p: p.name,
    )
    return candidates[0] if candidates else None


def _guess_start_command(backend_dir: Path) -> str:
    """Infer how to start the Python app from what the code imports."""
    if (backend_dir / "manage.py").is_file():
        return "python manage.py runserver 0.0.0.0:8000"

    for entry in ("main.py", "app.py", "server.py", "api.py", "run.py"):
        f = backend_dir / entry
        if not f.is_file():
            continue
        text = f.read_text(errors="ignore")
        module = entry[:-3]

        fastapi_var = re.search(r"^(\w+)\s*=\s*FastAPI\(", text, re.MULTILINE)
        if fastapi_var:
            return f"uvicorn {module}:{fastapi_var.group(1)} --host 0.0.0.0 --port 8000"

        flask_var = re.search(r"^(\w+)\s*=\s*Flask\(", text, re.MULTILINE)
        if flask_var:
            return (
                f"gunicorn --bind 0.0.0.0:8000 --workers 2 "
                f"{module}:{flask_var.group(1)}"
            )

        if "__main__" in text:
            return f"python {entry}"

    raise DetectionError(
        "We found Python code but could not tell how to start it. Add a "
        "launcher.yaml with a `backend.start` command, or make sure your app "
        "is created in main.py as `app = FastAPI()`."
    )


def _frontend_tool(pkg: dict, fe_dir: Path) -> str:
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    if "next" in deps:
        return "next"
    if "vite" in deps or list(fe_dir.glob("vite.config.*")):
        return "vite"
    if "react-scripts" in deps:
        return "cra"
    return "vite"


def _detect_backend(root: Path) -> BackendSpec | None:
    be_dir = _find_dir_containing(root, "requirements.txt", BACKEND_HINTS)
    req_name = "requirements.txt"
    if be_dir is None:
        be_dir = _find_dir_containing(root, "pyproject.toml", BACKEND_HINTS)
        req_name = None
    if be_dir is None:
        # Python files with no dependency manifest at all still count: a
        # stdlib-only backend is valid, just unusual.
        py = [p for p in root.glob("*.py")] + [p for p in root.glob("*/*.py")]
        if not py:
            return None
        be_dir = py[0].parent
        req_name = None

    rel = be_dir.relative_to(root).as_posix() or "."
    requirements = f"{rel}/{req_name}".lstrip("./") if req_name else None
    return BackendSpec(path=rel, start=_guess_start_command(be_dir), requirements=requirements)


def _detect_frontend(root: Path) -> FrontendSpec | None:
    fe_dir = _find_dir_containing(root, "package.json", FRONTEND_HINTS)
    if fe_dir is None:
        return None

    try:
        pkg = json.loads((fe_dir / "package.json").read_text(errors="ignore"))
    except json.JSONDecodeError as exc:
        raise DetectionError(
            f"Your package.json is not valid JSON ({exc.msg} on line {exc.lineno}). "
            "Ask your AI assistant to fix it and give you a new ZIP."
        ) from exc

    scripts = pkg.get("scripts", {})
    if "build" not in scripts:
        raise DetectionError(
            "Your package.json has no \"build\" command, so we cannot produce a "
            "production version of your dashboard. Ask your AI assistant to add "
            "a build script, then upload the new ZIP."
        )

    tool = _frontend_tool(pkg, fe_dir)
    return FrontendSpec(
        path=fe_dir.relative_to(root).as_posix() or ".",
        build="npm run build",
        output=BUILD_OUTPUT_BY_TOOL[tool],
    )


def _from_manifest(root: Path, data: dict) -> Spec:
    notes = ["Using settings from launcher.yaml"]
    backend = frontend = None

    be = data.get("backend")
    if isinstance(be, dict):
        path = be.get("path", ".").strip("/") or "."
        start = be.get("start")
        if not start:
            start = _guess_start_command(root / path)
        req = be.get("requirements")
        if req is None and (root / path / "requirements.txt").is_file():
            req = f"{path}/requirements.txt".lstrip("./")
        backend = BackendSpec(path=path, start=start, requirements=req)

    fe = data.get("frontend")
    if isinstance(fe, dict):
        path = fe.get("path", ".").strip("/") or "."
        frontend = FrontendSpec(
            path=path,
            build=fe.get("build", "npm run build"),
            output=fe.get("output", "dist"),
        )

    if backend is None and frontend is None:
        raise DetectionError(
            "Your launcher.yaml does not describe a backend or a frontend."
        )
    return Spec(kind=_kind(backend, frontend), backend=backend, frontend=frontend, notes=notes)


def _kind(backend: BackendSpec | None, frontend: FrontendSpec | None) -> str:
    if backend and frontend:
        return "fullstack"
    return "backend" if backend else "frontend"


def detect(root: Path) -> Spec:
    for manifest_name in ("launcher.yaml", "launcher.yml"):
        manifest = root / manifest_name
        if manifest.is_file():
            try:
                data = yaml.safe_load(manifest.read_text(errors="ignore")) or {}
            except yaml.YAMLError as exc:
                raise DetectionError(f"Your {manifest_name} could not be read: {exc}") from exc
            if not isinstance(data, dict):
                raise DetectionError(f"Your {manifest_name} should be a set of settings.")
            return _from_manifest(root, data)

    backend = _detect_backend(root)
    frontend = _detect_frontend(root)
    if backend is None and frontend is None:
        raise DetectionError(
            "We could not find an app in this ZIP. We look for a Python backend "
            "(requirements.txt) and/or a frontend (package.json). Make sure you "
            "zipped the project folder itself, not a folder containing it."
        )

    notes = []
    if backend:
        notes.append(f"Python backend in {backend.path}/ - starting with: {backend.start}")
    if frontend:
        notes.append(f"Frontend in {frontend.path}/ - building to {frontend.output}/")
    return Spec(kind=_kind(backend, frontend), backend=backend, frontend=frontend, notes=notes)
