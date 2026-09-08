import json
from pathlib import Path

import pytest

from launcher import detect


def write(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


PKG = json.dumps({
    "name": "web", "scripts": {"build": "vite build"},
    "dependencies": {"react": "18.3.1"}, "devDependencies": {"vite": "5.4.0"},
})


def test_detects_fullstack_layout(tmp_path):
    write(tmp_path, {
        "backend/requirements.txt": "fastapi==0.115.6\n",
        "backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "frontend/package.json": PKG,
    })
    spec = detect.detect(tmp_path)
    assert spec.kind == "fullstack"
    assert spec.backend.path == "backend"
    assert spec.backend.start == "uvicorn main:app --host 0.0.0.0 --port 8000"
    assert spec.backend.requirements == "backend/requirements.txt"
    assert spec.frontend.output == "dist"


def test_detects_backend_only(tmp_path):
    write(tmp_path, {
        "requirements.txt": "fastapi\n",
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    })
    spec = detect.detect(tmp_path)
    assert spec.kind == "backend"
    assert spec.frontend is None


def test_detects_frontend_only(tmp_path):
    write(tmp_path, {"package.json": PKG})
    spec = detect.detect(tmp_path)
    assert spec.kind == "frontend"
    assert spec.backend is None


def test_honours_non_default_fastapi_variable_name(tmp_path):
    write(tmp_path, {
        "requirements.txt": "fastapi\n",
        "main.py": "from fastapi import FastAPI\napi = FastAPI(title='x')\n",
    })
    spec = detect.detect(tmp_path)
    assert spec.backend.start.startswith("uvicorn main:api")


def test_detects_flask(tmp_path):
    write(tmp_path, {
        "requirements.txt": "flask\n",
        "app.py": "from flask import Flask\napp = Flask(__name__)\n",
    })
    spec = detect.detect(tmp_path)
    assert "gunicorn" in spec.backend.start
    assert "app:app" in spec.backend.start


def test_detects_django(tmp_path):
    write(tmp_path, {"requirements.txt": "django\n", "manage.py": "# django"})
    spec = detect.detect(tmp_path)
    assert "manage.py runserver" in spec.backend.start


def test_cra_build_output(tmp_path):
    pkg = json.dumps({
        "scripts": {"build": "react-scripts build"},
        "dependencies": {"react-scripts": "5.0.1"},
    })
    write(tmp_path, {"package.json": pkg})
    spec = detect.detect(tmp_path)
    assert spec.frontend.output == "build"


def test_missing_build_script_is_reported_plainly(tmp_path):
    pkg = json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"react": "18"}})
    write(tmp_path, {"package.json": pkg})
    with pytest.raises(detect.DetectionError, match='no "build" command'):
        detect.detect(tmp_path)


def test_invalid_package_json_is_reported_plainly(tmp_path):
    write(tmp_path, {"package.json": "{ not json,"})
    with pytest.raises(detect.DetectionError, match="not valid JSON"):
        detect.detect(tmp_path)


def test_empty_project_is_reported_plainly(tmp_path):
    write(tmp_path, {"README.md": "hello"})
    with pytest.raises(detect.DetectionError, match="could not find an app"):
        detect.detect(tmp_path)


def test_manifest_overrides_detection(tmp_path):
    write(tmp_path, {
        "launcher.yaml": (
            "backend:\n  path: ./svc\n  start: python serve.py\n"
            "frontend:\n  path: ./ui\n  output: build\n"
        ),
        "svc/serve.py": "print('hi')",
        "ui/package.json": PKG,
    })
    spec = detect.detect(tmp_path)
    assert spec.kind == "fullstack"
    assert spec.backend.start == "python serve.py"
    assert spec.frontend.output == "build"
