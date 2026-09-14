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


# --- optional settings ----------------------------------------------------

def test_a_setting_can_declare_itself_optional(tmp_path):
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": (
            "backend:\n  path: ./backend\n"
            "settings:\n"
            "  - name: REDASH_API_KEY\n"
            "  - name: SLACK_WEBHOOK\n    required: false\n"
        ),
    })
    spec = detect.detect(tmp_path)
    assert [(d.name, d.required) for d in spec.settings] == [
        ("REDASH_API_KEY", True), ("SLACK_WEBHOOK", False),
    ]


def test_settings_are_required_unless_they_say_otherwise(tmp_path):
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": "backend:\n  path: ./backend\nsettings:\n  - TOKEN\n",
    })
    assert detect.detect(tmp_path).settings[0].required is True


@pytest.mark.parametrize("written,expected", [
    ("false", False), ("no", False), ('"false"', False), ("0", False),
    ("true", True), ("yes", True), ('"true"', True),
])
def test_required_accepts_the_ways_people_write_it(tmp_path, written, expected):
    """Quoted values arrive as strings; treating "false" as true would hold an
    app back for a setting its author marked optional."""
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": (
            "backend:\n  path: ./backend\n"
            f"settings:\n  - name: TOKEN\n    required: {written}\n"
        ),
    })
    assert detect.detect(tmp_path).settings[0].required is expected


def test_a_nonsense_required_value_is_refused(tmp_path):
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": (
            "backend:\n  path: ./backend\n"
            "settings:\n  - name: TOKEN\n    required: sometimes\n"
        ),
    })
    with pytest.raises(detect.DetectionError, match="should be true or false"):
        detect.detect(tmp_path)


def test_an_optional_setting_does_not_hold_the_app_back(data_dir):
    db.init()
    app_id = db.create_app("alpha")
    db.set_declared_settings(app_id, [
        {"name": "REDASH_API_KEY", "description": "", "required": True},
        {"name": "SLACK_WEBHOOK", "description": "", "required": False},
    ])

    # Both unset: only the required one stops it starting.
    assert [d["name"] for d in db.unset_settings(app_id)] == [
        "REDASH_API_KEY", "SLACK_WEBHOOK",
    ]
    assert [d["name"] for d in db.missing_settings(app_id)] == ["REDASH_API_KEY"]

    db.set_setting(app_id, "REDASH_API_KEY", "secret")
    assert db.missing_settings(app_id) == [], "the optional one must not block"
    assert [d["name"] for d in db.unset_settings(app_id)] == ["SLACK_WEBHOOK"]


def test_older_records_without_the_field_stay_required(data_dir):
    """Declarations stored before optional settings existed must not suddenly
    stop holding their apps back."""
    db.init()
    app_id = db.create_app("alpha")
    db.set_declared_settings(app_id, [{"name": "TOKEN", "description": ""}])

    assert [d["name"] for d in db.missing_settings(app_id)] == ["TOKEN"]


# ---------------------------------------------------------------------------
# Settings the app brings its own value for
# ---------------------------------------------------------------------------

def test_a_default_is_read_from_the_manifest(tmp_path):
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": (
            "backend:\n  path: ./backend\n"
            "settings:\n"
            "  - name: TOKEN\n"
            "  - name: PAGE_SIZE\n    default: 50\n"
            "  - name: VERBOSE\n    default: true\n"
        ),
    })
    spec = detect.detect(tmp_path)
    by_name = {d.name: d for d in spec.settings}

    assert by_name["TOKEN"].default is None
    # YAML hands back an int and a bool; both have to survive as environment
    # variables, which are text.
    assert by_name["PAGE_SIZE"].default == "50"
    assert by_name["VERBOSE"].default == "true"


def test_an_empty_default_is_refused(tmp_path):
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": (
            "backend:\n  path: ./backend\n"
            "settings:\n  - name: TOKEN\n    default: \"\"\n"
        ),
    })
    with pytest.raises(detect.DetectionError, match="is empty"):
        detect.detect(tmp_path)


def test_a_list_default_is_refused(tmp_path):
    write(tmp_path, {
        **BACKEND,
        "launcher.yaml": (
            "backend:\n  path: ./backend\n"
            "settings:\n  - name: HOSTS\n    default:\n      - a\n      - b\n"
        ),
    })
    with pytest.raises(detect.DetectionError, match="single value"):
        detect.detect(tmp_path)


def test_a_defaulted_setting_is_never_waited_for(data_dir):
    db.init()
    app_id = db.create_app("alpha")
    db.set_declared_settings(app_id, [
        {"name": "TOKEN", "description": "", "required": True, "default": None},
        {"name": "PAGE_SIZE", "description": "", "required": True, "default": "50"},
    ])

    # PAGE_SIZE is declared required, but the app answered its own question.
    assert [d["name"] for d in db.missing_settings(app_id)] == ["TOKEN"]
    assert [d["name"] for d in db.unset_settings(app_id)] == ["TOKEN"]


def test_the_app_runs_with_its_defaults_until_they_are_overridden(data_dir):
    db.init()
    app_id = db.create_app("alpha")
    db.set_declared_settings(app_id, [
        {"name": "PAGE_SIZE", "description": "", "required": True, "default": "50"},
        {"name": "TOKEN", "description": "", "required": True, "default": None},
    ])
    db.set_setting(app_id, "TOKEN", "abc123")

    assert db.effective_env(app_id) == {"PAGE_SIZE": "50", "TOKEN": "abc123"}

    db.set_setting(app_id, "PAGE_SIZE", "200")
    assert db.effective_env(app_id)["PAGE_SIZE"] == "200"

    # Removing the override is how you get back to what the ZIP shipped with.
    db.delete_setting(app_id, "PAGE_SIZE")
    assert db.effective_env(app_id)["PAGE_SIZE"] == "50"
    assert db.missing_settings(app_id) == [], "a default is not a missing value"


def test_a_default_never_displaces_the_launchers_own_variables(data_dir):
    """An app declaring `PORT: 8000` as a default must not be able to point
    itself away from the port it was given."""
    db.init()
    app_id = db.create_app("alpha")
    db.set_declared_settings(app_id, [
        {"name": "PORT", "description": "", "required": False, "default": "8000"},
    ])

    env = settings.environment(
        db.effective_env(app_id), {"PORT": "31234", "APP_DATA_DIR": "/data"}
    )
    assert env["PORT"] == "31234"


# ---------------------------------------------------------------------------
# The hold has to survive every route into launch(), not just the deploy
# ---------------------------------------------------------------------------

def test_pressing_start_cannot_bypass_the_hold(data_dir, monkeypatch, tmp_path):
    """Start used to call launch() straight through, so an app waiting for six
    settings came up serving with all six empty - reporting itself live while
    being exactly as unusable as the hold exists to prevent."""
    from launcher import config, native, supervisor

    db.init()
    app_id = db.create_app("query-allocation-dashboard")
    db.set_declared_settings(app_id, [
        {"name": "REDASH_QUERY_API_KEY", "description": "", "required": True},
        {"name": "REDASH_API_ROOT", "description": "", "required": True},
    ])
    db.update_app(app_id, status="needs_setup")

    src = config.SRC_DIR / "query-allocation-dashboard"
    src.mkdir(parents=True, exist_ok=True)
    (src / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (src / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )

    started = []

    def fake_start(*args, **kwargs):
        started.append(args)
        # The real one hands back the pids it spawned; returning None here
        # would fail for a reason that has nothing to do with the hold.
        return native.Processes(front_pid=4242, backend_pid=4243)

    monkeypatch.setattr(native, "start", fake_start)

    assert supervisor.launch(db.get_app(app_id), reason="Started from the dashboard.") is False
    assert started == [], "nothing may be started while a declared setting is unset"
    assert db.get_app(app_id)["status"] == "needs_setup"

    # Supplying them is what opens the gate.
    db.set_setting(app_id, "REDASH_QUERY_API_KEY", "k")
    db.set_setting(app_id, "REDASH_API_ROOT", "https://redash.example")
    assert supervisor.launch(db.get_app(app_id)) is True
    assert started, "with its settings set it starts normally"
