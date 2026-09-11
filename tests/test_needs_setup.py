"""An app is never started without the settings it said it needs.

The state being avoided: built, started, reporting itself live, and unusable
because a key it declared was never supplied.
"""
import json

import pytest

from launcher import db, detect, settings


def write(root, files):
    for name, content in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


BACKEND = {
    "backend/requirements.txt": "fastapi\n",
    "backend/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
}


# --- declaring ------------------------------------------------------------

def test_settings_can_be_declared_as_plain_names(tmp_path):
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": "backend:\n  path: ./backend\nsettings:\n  - REDASH_API_KEY\n",
    })
    spec = detect.detect(tmp_path)
    assert [d.name for d in spec.settings] == ["REDASH_API_KEY"]


def test_settings_can_carry_a_description(tmp_path):
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": (
            "backend:\n  path: ./backend\n"
            "settings:\n  - name: REDASH_API_KEY\n    description: From Redash, under Profile\n"
        ),
    })
    spec = detect.detect(tmp_path)
    assert spec.settings[0].description == "From Redash, under Profile"


def test_a_mistyped_name_is_caught_at_deploy_time(tmp_path):
    """Otherwise it surfaces as the app not finding its own configuration."""
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": "backend:\n  path: ./backend\nsettings:\n  - 2FA TOKEN\n",
    })
    with pytest.raises(detect.DetectionError, match="not a usable name"):
        detect.detect(tmp_path)


def test_a_reserved_name_is_refused(tmp_path):
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": "backend:\n  path: ./backend\nsettings:\n  - APP_DATA_DIR\n",
    })
    with pytest.raises(detect.DetectionError, match="set by the server itself"):
        detect.detect(tmp_path)


def test_the_same_setting_twice_is_refused(tmp_path):
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": "backend:\n  path: ./backend\nsettings:\n  - TOKEN\n  - TOKEN\n",
    })
    with pytest.raises(detect.DetectionError, match="twice"):
        detect.detect(tmp_path)


def test_declaring_nothing_is_normal(tmp_path):
    write(tmp_path, BACKEND)
    assert detect.detect(tmp_path).settings == []


# --- what is outstanding --------------------------------------------------

@pytest.fixture
def app_id(data_dir):
    db.init()
    app_id = db.create_app("alpha")
    db.set_declared_settings(app_id, [
        {"name": "REDASH_API_KEY", "description": "From Redash"},
        {"name": "REPORT_EMAIL", "description": ""},
    ])
    return app_id


def test_everything_declared_is_outstanding_at_first(app_id):
    assert [d["name"] for d in db.missing_settings(app_id)] == [
        "REDASH_API_KEY", "REPORT_EMAIL",
    ]


def test_supplying_one_leaves_the_other(app_id):
    db.set_setting(app_id, "REDASH_API_KEY", "secret")
    assert [d["name"] for d in db.missing_settings(app_id)] == ["REPORT_EMAIL"]


def test_nothing_outstanding_once_all_are_set(app_id):
    db.set_setting(app_id, "REDASH_API_KEY", "secret")
    db.set_setting(app_id, "REPORT_EMAIL", "team@example.com")
    assert db.missing_settings(app_id) == []


def test_settings_not_declared_do_not_make_an_app_wait(data_dir):
    """Extra values are fine; only declared-and-unset holds an app back."""
    db.init()
    app_id = db.create_app("beta")
    db.set_setting(app_id, "SOMETHING_EXTRA", "value")
    assert db.missing_settings(app_id) == []


def test_a_redeploy_replaces_what_the_app_declares(app_id):
    db.set_declared_settings(app_id, [{"name": "ONLY_THIS", "description": ""}])
    assert [d["name"] for d in db.missing_settings(app_id)] == ["ONLY_THIS"]


def test_corrupt_declarations_do_not_block_an_app(app_id):
    """A bad value in the column must not make the app permanently unstartable."""
    db.update_app(app_id, declared_settings="not json at all")
    assert db.missing_settings(app_id) == []
