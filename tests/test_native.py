"""Native runtime: turning a detected start command into a runnable one."""
import json
from pathlib import Path

import pytest

from launcher import config, detect, native


def project(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


FASTAPI = {
    "requirements.txt": "fastapi\n",
    "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
}


def test_fastapi_command_is_rebound_to_the_apps_own_port(tmp_path):
    src = project(tmp_path, FASTAPI)
    cmd = native.backend_command(detect.detect(src), src, 30412)

    assert "--port" in cmd and "30412" in cmd
    assert "8000" not in cmd, "the placeholder port must not survive"
    assert str(native.venv_python(src)) == cmd[0], "must run in the app's own venv"


def test_backend_is_bound_to_loopback_not_the_network(tmp_path):
    """The backend is reached through the front server, never directly."""
    src = project(tmp_path, FASTAPI)
    cmd = native.backend_command(detect.detect(src), src, 30412)

    assert "0.0.0.0" not in " ".join(cmd)
    assert "127.0.0.1" in cmd


def test_flask_uses_uvicorn_because_gunicorn_has_no_windows_support(tmp_path):
    src = project(tmp_path, {
        "requirements.txt": "flask\n",
        "app.py": "from flask import Flask\napp = Flask(__name__)\n",
    })
    cmd = native.backend_command(detect.detect(src), src, 30500)

    assert "gunicorn" not in " ".join(cmd)
    assert "uvicorn" in cmd
    assert "--interface" in cmd and "wsgi" in cmd
    assert "app:app" in cmd
    assert "30500" in cmd


def test_django_runserver_is_rebound(tmp_path):
    src = project(tmp_path, {"requirements.txt": "django\n", "manage.py": "# django"})
    cmd = native.backend_command(detect.detect(src), src, 30600)

    assert "127.0.0.1:30600" in cmd
    assert "0.0.0.0:8000" not in " ".join(cmd)


def test_manifest_port_placeholder_is_substituted(tmp_path):
    src = project(tmp_path, {
        "launcher.yaml": "backend:\n  path: .\n  start: python serve.py --port $PORT\n",
        "serve.py": "print('hi')\n",
    })
    cmd = native.backend_command(detect.detect(src), src, 30700)

    assert "$PORT" not in " ".join(cmd)
    assert "30700" in cmd


def test_venv_python_matches_the_platform_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    assert native.venv_python(tmp_path).as_posix().endswith("Scripts/python.exe")

    monkeypatch.setattr(config, "IS_WINDOWS", False)
    assert native.venv_python(tmp_path).as_posix().endswith("bin/python")


def test_is_running_rejects_a_reused_pid(monkeypatch):
    """A pid alone is not proof: the OS reuses numbers for other programs."""
    import os
    assert native.is_running(os.getpid(), marker="pytest") is True
    assert native.is_running(os.getpid(), marker="definitely-not-this-process") is False


def test_is_running_handles_absent_pids():
    assert native.is_running(None) is False
    assert native.is_running(0) is False


def test_missing_npm_is_reported_as_a_server_problem(monkeypatch):
    monkeypatch.setattr(config, "NPM_COMMAND", "")
    monkeypatch.setattr(native.shutil, "which", lambda name: None)

    with pytest.raises(native.ToolchainError, match="administrator rights"):
        native.npm_command()

    assert native.toolchain_report()["npm"] is None


def test_static_dir_points_at_the_build_output(tmp_path):
    src = project(tmp_path, {
        "package.json": json.dumps({"scripts": {"build": "vite build"}}),
    })
    spec = detect.detect(src)
    assert native.static_dir(src, spec) == src / "dist"


# ---------------------------------------------------------------------------
# Build output: what the log actually shows
# ---------------------------------------------------------------------------

def test_build_output_is_read_as_utf8_not_the_machine_locale(tmp_path):
    """Vite prints a tick and box-drawing bars. Decoding those with the
    Windows locale codec turned every successful build log into mojibake:
    U+2713 arrived as "Ã¢Å“â€œ" on the real server.

    This passes either way where the locale is already UTF-8, which is why
    the companion test below checks the source rather than the behaviour -
    the bug only appears on the machines we cannot run the suite on.
    """
    import sys
    seen: list[str] = []
    code = native._stream(
        [sys.executable, "-c",
         "import sys; sys.stdout.buffer.write('\\u2713 built \\u2502 42 kB\\n'.encode())"],
        tmp_path, seen.append, 60,
    )

    assert code == 0
    assert "✓ built │ 42 kB" in "".join(seen)


def test_a_frontend_without_a_lockfile_is_not_run_through_npm_ci(tmp_path, monkeypatch):
    """npm ci refuses without a lockfile and prints its whole usage page to do
    it, so a normal deploy read as forty lines of "npm error"."""
    src = project(tmp_path, {
        "frontend/package.json": json.dumps({"scripts": {"build": "vite build"}}),
    })
    (src / "frontend" / "dist").mkdir(parents=True)
    (src / "frontend" / "dist" / "index.html").write_text("<html></html>")

    calls: list[list[str]] = []
    monkeypatch.setattr(native, "npm_command", lambda: "npm")
    monkeypatch.setattr(native, "_stream",
                        lambda cmd, cwd, sink, timeout, env=None: calls.append(cmd) or 0)

    spec = detect.Spec(
        kind="frontend", backend=None,
        frontend=detect.FrontendSpec(path="frontend", build="npm run build",
                                     output="dist"),
        notes=[],
    )
    assert native.prepare(src, spec, lambda _t: None) == 0

    assert ["npm", "ci", "--no-audit", "--no-fund"] not in calls
    assert ["npm", "install", "--no-audit", "--no-fund"] in calls


def test_a_frontend_with_a_lockfile_still_gets_the_reproducible_install(
    tmp_path, monkeypatch
):
    src = project(tmp_path, {
        "frontend/package.json": json.dumps({"scripts": {"build": "vite build"}}),
        "frontend/package-lock.json": json.dumps({"lockfileVersion": 3}),
    })
    (src / "frontend" / "dist").mkdir(parents=True)
    (src / "frontend" / "dist" / "index.html").write_text("<html></html>")

    calls: list[list[str]] = []
    monkeypatch.setattr(native, "npm_command", lambda: "npm")
    monkeypatch.setattr(native, "_stream",
                        lambda cmd, cwd, sink, timeout, env=None: calls.append(cmd) or 0)

    spec = detect.Spec(
        kind="frontend", backend=None,
        frontend=detect.FrontendSpec(path="frontend", build="npm run build",
                                     output="dist"),
        notes=[],
    )
    assert native.prepare(src, spec, lambda _t: None) == 0
    assert ["npm", "ci", "--no-audit", "--no-fund"] in calls


def test_no_text_file_is_read_at_the_mercy_of_the_platform_encoding():
    """The launcher writes logs as UTF-8 but ran on Windows servers whose
    default codec is cp1252, so reading one back mangled it a second time -
    a vite tick arrived in the log as "Ã¢Å“â€œ".

    Every text read and write has to name its encoding. Checked against the
    source because the fault needs a non-UTF-8 locale to show itself, and the
    suite never runs on one.
    """
    import ast

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "launcher").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in ("read_text", "write_text", "open"):
                continue
            # ZipFile.open and the like always hand back bytes.
            if name == "open" and not isinstance(node.func, ast.Name):
                continue
            kwargs = {k.arg for k in node.keywords}
            if "encoding" in kwargs:
                continue
            # Binary mode never decodes, so it cannot be mangled.
            mode = next((a for a in node.args[1:2] if isinstance(a, ast.Constant)), None)
            if name == "open" and mode is not None and "b" in str(mode.value):
                continue
            offenders.append(f"{path.name}:{node.lineno}: {name}(...)")

    assert not offenders, (
        "these read or write text without naming an encoding:\n"
        + "\n".join(offenders)
    )
