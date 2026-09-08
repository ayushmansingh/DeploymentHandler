"""The self-test's sample project must stay compatible with the detector.

If detection rules change and the sample stops being recognised, the self-test
would report a broken install on a healthy server - so pin the relationship
here, where it fails fast and without Docker.
"""
import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

from launcher import archive, detect, imagegen

SELFTEST = Path(__file__).resolve().parents[1] / "scripts" / "selftest.py"


@pytest.fixture(scope="module")
def selftest_module():
    spec = importlib.util.spec_from_file_location("selftest", SELFTEST)
    module = importlib.util.module_from_spec(spec)
    sys.modules["selftest"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("quick", [True, False])
def test_sample_is_detected_as_fullstack(selftest_module, tmp_path, quick):
    project = tmp_path / "project"
    selftest_module.write_sample(project, quick=quick)

    spec = detect.detect(project)
    assert spec.kind == "fullstack"
    assert spec.backend.start == "uvicorn main:app --host 0.0.0.0 --port 8000"
    assert spec.frontend.output == "dist", (
        "the sample's build output must match what the detector expects"
    )


def test_sample_survives_a_zip_round_trip(selftest_module, tmp_path):
    project = tmp_path / "project"
    selftest_module.write_sample(project, quick=True)
    zip_path = selftest_module.make_zip(project, tmp_path / "sample.zip")

    assert zipfile.is_zipfile(zip_path)
    extracted = archive.extract(zip_path, tmp_path / "out")
    assert extracted.unwrapped_from is None, "the sample must not need unwrapping"
    assert detect.detect(extracted.root).kind == "fullstack"


def test_generated_dockerfile_wires_both_halves(selftest_module, tmp_path):
    project = tmp_path / "project"
    selftest_module.write_sample(project, quick=True)
    spec = detect.detect(project)
    imagegen.write_build_context(project, spec)

    dockerfile = (project / "Dockerfile").read_text()
    assert "COPY --from=frontend /fe/dist/ /var/www/html/" in dockerfile
    assert "COPY backend/requirements.txt" in dockerfile

    nginx = (project / ".launcher" / "nginx.conf").read_text()
    assert "location /api/" in nginx
    assert "proxy_pass http://127.0.0.1:8000;" in nginx

    start = (project / ".launcher" / "start.sh").read_text()
    assert "uvicorn main:app" in start
    assert "\r\n" not in start, "CRLF line endings break the container entrypoint"


def test_sample_backend_answers_the_route_the_selftest_checks(selftest_module, tmp_path):
    """The marker the self-test greps for must actually be served."""
    project = tmp_path / "project"
    selftest_module.write_sample(project, quick=True)

    backend = (project / "backend" / "main.py").read_text()
    assert "/api/ping" in backend
    assert selftest_module.MARKER in backend
    assert selftest_module.MARKER in (project / "frontend" / "index.html").read_text()
