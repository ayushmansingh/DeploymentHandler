"""Per-app settings: how an app gets an API key without anyone touching the server.

The property that matters most: a value can be set and replaced from the
dashboard, but never read back out of it.
"""
import pytest

from launcher import db, settings


# --- validation -----------------------------------------------------------

@pytest.mark.parametrize("key", ["REDASH_API_KEY", "_private", "Api_Key_2", "x"])
def test_accepts_usable_names(key):
    assert settings.clean_key(key) == key


def test_names_are_kept_exactly_as_typed():
    """Upper-casing would stop os.environ["Api_Key"] finding it."""
    assert settings.clean_key("  Api_Key  ") == "Api_Key"


@pytest.mark.parametrize("key", ["2FA_TOKEN", "api key", "api-key", "", "  ", "a" * 70])
def test_rejects_names_a_shell_could_not_use(key):
    with pytest.raises(settings.InvalidSetting):
        settings.clean_key(key)


@pytest.mark.parametrize("key", sorted(settings.RESERVED))
def test_rejects_names_the_launcher_owns(key):
    with pytest.raises(settings.InvalidSetting, match="set by the server itself"):
        settings.clean_key(key)


def test_values_lose_pasted_whitespace():
    assert settings.clean_value("  secret123\n") == "secret123"


def test_rejects_an_empty_value():
    with pytest.raises(settings.InvalidSetting, match="Give the setting a value"):
        settings.clean_value("   ")


def test_rejects_an_oversized_value():
    with pytest.raises(settings.InvalidSetting, match="too long"):
        settings.clean_value("x" * (settings.MAX_VALUE_LENGTH + 1))


# --- environment ----------------------------------------------------------

def test_settings_reach_the_app():
    env = settings.environment({"REDASH_API_KEY": "abc"}, {"PORT": "30412"})
    assert env["REDASH_API_KEY"] == "abc"


def test_a_setting_cannot_displace_the_launchers_own_variables():
    """Overriding PORT would break the app; APP_DATA_DIR would lose its files."""
    env = settings.environment(
        {"PORT": "1", "APP_DATA_DIR": "/tmp/elsewhere", "OK": "yes"},
        {"PORT": "30412", "APP_DATA_DIR": "/real/store"},
    )
    assert env["PORT"] == "30412"
    assert env["APP_DATA_DIR"] == "/real/store"
    assert env["OK"] == "yes"


# --- storage --------------------------------------------------------------

@pytest.fixture
def app_id(data_dir):
    db.init()
    return db.create_app("alpha")


def test_saving_then_launching_hands_the_value_over(app_id):
    db.set_setting(app_id, "REDASH_API_KEY", "secret123", "Priya")
    assert db.settings_env(app_id) == {"REDASH_API_KEY": "secret123"}


def test_the_listing_never_includes_values(app_id):
    """The dashboard renders this, so a value must not be reachable from it."""
    db.set_setting(app_id, "REDASH_API_KEY", "secret123")

    rows = db.list_setting_keys(app_id)

    assert [r["key"] for r in rows] == ["REDASH_API_KEY"]
    assert "value" not in rows[0].keys()
    assert "secret123" not in str([dict(r) for r in rows])


def test_saving_the_same_name_replaces_the_value(app_id):
    db.set_setting(app_id, "TOKEN", "old", "Priya")
    db.set_setting(app_id, "TOKEN", "new", "Sam")

    assert db.settings_env(app_id) == {"TOKEN": "new"}
    rows = db.list_setting_keys(app_id)
    assert len(rows) == 1 and rows[0]["updated_by"] == "Sam"


def test_removing_a_setting(app_id):
    db.set_setting(app_id, "TOKEN", "secret")
    db.delete_setting(app_id, "TOKEN")

    assert db.settings_env(app_id) == {}


def test_settings_belong_to_one_app_only(app_id, data_dir):
    other = db.create_app("beta")
    db.set_setting(app_id, "TOKEN", "alpha-secret")
    db.set_setting(other, "TOKEN", "beta-secret")

    assert db.settings_env(app_id)["TOKEN"] == "alpha-secret"
    assert db.settings_env(other)["TOKEN"] == "beta-secret"


def test_deleting_an_app_deletes_its_settings(app_id):
    db.set_setting(app_id, "TOKEN", "secret")
    db.delete_app(app_id)

    assert db.settings_env(app_id) == {}
