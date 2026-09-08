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
