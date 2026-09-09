"""Storage that outlives a deploy.

The property under test throughout: a file the app wrote is still there after
the app is replaced with a new ZIP.
"""
import os
from pathlib import Path

import pytest

from launcher import appdata, archive, config, detect

pytestmark = pytest.mark.usefixtures("data_dir")


def make_project(root: Path, extra: dict[str, str] | None = None) -> Path:
    files = {
        "backend/requirements.txt": "fastapi\n",
        "backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        **(extra or {}),
    }
    for name, content in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def test_store_is_outside_the_source_tree(tmp_path):
    """If it lived under src/ the extract would wipe it every deploy."""
    store = appdata.dir_for("alpha")
    assert store.is_dir()
    assert config.SRC_DIR not in store.parents


def test_data_link_reaches_the_store(tmp_path):
    src = make_project(tmp_path / "src")
    spec = detect.detect(src)
    store = appdata.attach("alpha", src, spec, lambda _: None)

    (src / "backend" / "data" / "report.csv").write_text("a,b\n1,2\n")
    assert (store / "report.csv").read_text() == "a,b\n1,2\n"


def test_files_survive_being_replaced_by_a_new_zip(tmp_path):
    """The whole point: pull data from Redash, then update the dashboard."""
    src = config.SRC_DIR / "alpha"
    make_project(src)
    spec = detect.detect(src)
    store = appdata.attach("alpha", src, spec, lambda _: None)

    # The running app collects something worth keeping.
    (store / "redash_export.csv").write_text("rows\n1000\n")

    # A newer ZIP arrives: the deploy detaches, wipes and rebuilds the source.
    appdata.detach(src)
    new_zip = tmp_path / "v2.zip"
    import zipfile
    with zipfile.ZipFile(new_zip, "w") as zf:
        zf.writestr("backend/requirements.txt", "fastapi\n")
        zf.writestr("backend/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        zf.writestr("frontend/package.json", '{"scripts":{"build":"vite build"}}')
    archive.extract(new_zip, src)
    spec = detect.detect(src)
    appdata.attach("alpha", src, spec, lambda _: None)

    assert (store / "redash_export.csv").read_text() == "rows\n1000\n"
    reachable = src / spec.backend.path / "data" / "redash_export.csv"
    assert reachable.is_file(), "the new version must still see the saved file"


def test_seed_data_in_the_zip_is_moved_into_the_store(tmp_path):
    src = make_project(tmp_path / "src", {"backend/data/seed.csv": "x\n1\n"})
    spec = detect.detect(src)
    store = appdata.attach("alpha", src, spec, lambda _: None)

    assert (store / "seed.csv").read_text() == "x\n1\n"


def test_a_later_upload_does_not_overwrite_collected_data(tmp_path):
    """Seed files in the ZIP must not clobber what the app has since written."""
    src = config.SRC_DIR / "alpha"
    make_project(src, {"backend/data/seed.csv": "original\n"})
    store = appdata.attach("alpha", src, detect.detect(src), lambda _: None)
    (store / "seed.csv").write_text("updated by the app\n")

    appdata.detach(src)
    make_project(src, {"backend/data/seed.csv": "original\n"})
    appdata.attach("alpha", src, detect.detect(src), lambda _: None)

    assert (store / "seed.csv").read_text() == "updated by the app\n"


def test_detach_leaves_the_stored_files_alone(tmp_path):
    """Detaching must remove the link, never what it points at."""
    src = make_project(tmp_path / "src")
    store = appdata.attach("alpha", src, detect.detect(src), lambda _: None)
    (store / "keep.csv").write_text("data\n")

    appdata.detach(src)

    assert not (src / "backend" / "data").exists()
    assert (store / "keep.csv").read_text() == "data\n"


def test_size_reporting(tmp_path):
    store = appdata.dir_for("alpha")
    assert appdata.size_bytes("alpha") == 0
    assert appdata.human_size(0) == "empty"

    (store / "big.csv").write_text("x" * 4096)
    assert appdata.size_bytes("alpha") == 4096
    assert "KB" in appdata.human_size(appdata.size_bytes("alpha"))


def test_removing_an_app_removes_its_data(tmp_path):
    store = appdata.dir_for("alpha")
    (store / "report.csv").write_text("data\n")

    appdata.remove("alpha")

    assert not store.exists()


def test_frontend_only_app_still_gets_a_store(tmp_path):
    import json
    src = tmp_path / "src"
    (src).mkdir()
    (src / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))

    store = appdata.attach("alpha", src, detect.detect(src), lambda _: None)
    assert store.is_dir()
