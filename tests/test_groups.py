"""Filing apps under groups, and the tabs that show them.

The dashboard grows a row per deploy and nobody prunes it, so the point of
this is finding one app among thirty. Everything here is about that: the tabs
are a filter over one list, not a second navigation, and no app can fall out
of sight because All always holds every one of them.
"""
import pytest
from fastapi.testclient import TestClient

from launcher import config, db, deployer


@pytest.fixture
def client(data_dir, monkeypatch):
    """A client whose deploys are recorded rather than built.

    Groups are decided the moment the form posts, so the build that follows
    is beside the point here - and letting it run would have a worker thread
    writing into the temp directory while the fixture tears it down.
    """
    from launcher import app as app_module

    monkeypatch.setattr(config, "PASSWORD", "")
    monkeypatch.setattr(deployer, "enqueue", lambda deploy_id: None)
    db.init()
    return TestClient(app_module.app, follow_redirects=False)


def make(name: str, groups: list[str], status: str = "live") -> int:
    app_id = db.create_app(name, owner="Priya")
    db.update_app(app_id, status=status, host_port=24800 + app_id,
                  backend_port=30400 + app_id)
    db.set_app_groups(app_id, groups)
    return app_id


@pytest.fixture
def three(client):
    make("sales-dashboard", ["Dashboards"])
    make("zip-tool", ["Tools"])
    make("brand-kit", ["Designs", "Tools"])
    make("loose-app", [])
    return client


def cards(html: str) -> str:
    """Just the grid, so a name in a tab link is not mistaken for a card."""
    _, _, rest = html.partition('<div class="grid">')
    return rest


# ---------------------------------------------------------------------------
# Storing them
# ---------------------------------------------------------------------------

def test_an_app_can_be_in_more_than_one_group(client):
    app_id = make("brand-kit", ["Designs", "Tools"])
    assert db.app_groups(app_id) == ["Designs", "Tools"]


def test_setting_groups_replaces_rather_than_adds(client):
    """Unticking the last box has to be able to leave an app with none."""
    app_id = make("brand-kit", ["Designs", "Tools"])

    db.set_app_groups(app_id, [])

    assert db.app_groups(app_id) == []


def test_the_same_group_twice_is_stored_once(client):
    app_id = make("zip-tool", ["Tools", "Tools"])
    assert db.app_groups(app_id) == ["Tools"]


def test_deleting_an_app_takes_its_groups_with_it(client):
    app_id = make("zip-tool", ["Tools"])

    db.delete_app(app_id)

    assert db.groups_by_app() == {}


def test_every_apps_groups_come_back_in_one_query(three):
    """The grid draws every card and polls itself, so a query per app would
    be a round trip per app per refresh for a value this small."""
    by_app = db.groups_by_app()
    assert sorted(len(v) for v in by_app.values()) == [1, 1, 2]


# ---------------------------------------------------------------------------
# The tabs
# ---------------------------------------------------------------------------

def test_all_shows_every_app_including_ungrouped_ones(three):
    body = three.get("/").text
    for name in ("sales-dashboard", "zip-tool", "brand-kit", "loose-app"):
        assert name in cards(body), name


def test_a_group_tab_shows_only_that_group(three):
    body = cards(three.get("/?group=Tools").text)

    assert "zip-tool" in body
    assert "brand-kit" in body, "an app in two groups shows under both"
    assert "sales-dashboard" not in body
    assert "loose-app" not in body


def test_an_app_with_no_group_is_still_reachable(three):
    """Nothing may become invisible by being filed nowhere."""
    assert "loose-app" in cards(three.get("/").text)


def tab_counts(html: str) -> dict[str, int]:
    """The name and number on each tab, read back out of the rendered bar."""
    import re
    bar = re.search(r'<nav class="grouptabs">(.*?)</nav>', html, re.S)
    assert bar, "the tab bar is missing"
    pairs = re.findall(r'>([A-Za-z ]+)<span\s+class="count">(\d+)<', bar.group(1))
    return {name: int(count) for name, count in pairs}


def test_the_tab_counts_come_from_every_app_not_the_filtered_ones(three):
    """Otherwise every tab but the current one would read zero, and a tab
    would vanish from under the person standing on it."""
    counts = tab_counts(three.get("/?group=Designs").text)

    assert counts == {"All": 4, "Dashboards": 1, "Tools": 2, "Designs": 1}


def test_all_counts_every_app_and_a_group_counts_its_own(three):
    counts = tab_counts(three.get("/").text)

    assert counts["All"] == 4, "including the one filed nowhere"
    assert counts["Tools"] == 2, "an app in two groups counts in both"


def test_an_unknown_group_falls_back_to_all(three):
    """A bookmark outlives a change to the configured list; an empty page
    with no explanation is the wrong answer to it."""
    body = cards(three.get("/?group=Nonsense").text)
    assert "sales-dashboard" in body and "zip-tool" in body


def test_an_empty_group_says_what_to_do(client):
    make("loose-app", [])

    body = client.get("/?group=Designs").text

    assert "Nothing is filed under Designs yet" in body


def test_the_live_refresh_stays_on_the_chosen_tab(three):
    """The poll runs every few seconds. If it dropped the group it would walk
    the person back to All moments after they picked a tab."""
    body = cards(three.get("/partials/apps?group=Designs").text)

    assert "brand-kit" in body
    assert "zip-tool" not in body


def test_a_build_on_another_tab_still_counts_as_busy(three):
    """Busy sets the poll interval. Asking only the visible apps would let a
    build elsewhere finish at the slow rate, so it would look stuck."""
    building = db.get_app_by_name("sales-dashboard")
    db.update_app(int(building["id"]), status="building")

    body = three.get("/?group=Tools").text

    assert 'data-busy="1"' in body


def test_the_machines_own_numbers_are_not_filtered(three):
    """"Memory used by the Tools tab" would mean nothing."""
    everything = three.get("/").text
    one_tab = three.get("/?group=Tools").text

    for body in (everything, one_tab):
        assert "Server memory" in body and "Disk" in body


# ---------------------------------------------------------------------------
# Choosing them
# ---------------------------------------------------------------------------

def test_the_deploy_form_offers_the_groups(client):
    body = client.get("/deploy").text
    assert 'name="groups"' in body
    for name in config.GROUPS:
        assert f'value="{name}"' in body


def test_an_existing_app_can_be_filed_from_its_own_page(client):
    make("loose-app", [])

    response = client.post("/app/loose-app/groups",
                           data={"groups": ["Tools", "Designs"]})

    assert response.status_code == 303
    app_id = int(db.get_app_by_name("loose-app")["id"])
    assert db.app_groups(app_id) == ["Designs", "Tools"]


def test_saving_none_clears_them(client):
    make("zip-tool", ["Tools"])

    client.post("/app/zip-tool/groups", data={})

    assert db.app_groups(int(db.get_app_by_name("zip-tool")["id"])) == []


def test_the_manage_page_shows_what_is_ticked(client):
    make("brand-kit", ["Designs"])

    body = client.get("/app/brand-kit").text

    assert 'value="Designs" checked' in body
    assert 'value="Tools" checked' not in body


def test_a_group_this_server_does_not_offer_is_refused(client):
    """A form can post anything, and the configured list can change under a
    page left open. Either way it must not put a group on the dashboard that
    has no tab to reach it by."""
    make("zip-tool", [])

    client.post("/app/zip-tool/groups",
                data={"groups": ["Tools", "Nonsense", "<script>"]})

    assert db.app_groups(int(db.get_app_by_name("zip-tool")["id"])) == ["Tools"]


def test_groups_are_ordered_as_the_server_lists_them(client):
    """So two apps in the same groups never show them in a different order."""
    app_id = make("brand-kit", [])

    for order in (["Designs", "Dashboards"], ["Dashboards", "Designs"]):
        db.set_app_groups(app_id, order)
        from launcher import app as app_module
        assert app_module._clean_groups(db.app_groups(app_id)) == [
            "Dashboards", "Designs"]


def test_the_configured_list_can_be_changed(client, monkeypatch):
    monkeypatch.setattr(config, "GROUPS", ("Reports", "Toys"))

    body = client.get("/deploy").text

    assert 'value="Reports"' in body
    assert 'value="Dashboards"' not in body


def test_a_group_dropped_from_the_list_is_kept_not_destroyed(client, monkeypatch):
    """Removing a group from the configured list is a display change. If it
    deleted the filing, putting the group back would not bring it back."""
    app_id = make("brand-kit", ["Designs"])

    monkeypatch.setattr(config, "GROUPS", ("Dashboards", "Tools"))
    assert db.app_groups(app_id) == ["Designs"], "still on the record"

    monkeypatch.setattr(config, "GROUPS", ("Dashboards", "Tools", "Designs"))
    body = client.get("/?group=Designs").text
    assert "brand-kit" in cards(body), "and shows again once restored"


# ---------------------------------------------------------------------------
# Deploying into a group
# ---------------------------------------------------------------------------

def upload(client, name: str, groups: list[str] | None = None):
    """A deploy through the real form, with the build stubbed out."""
    return client.post(
        "/upload",
        data={"name": name, "uploaded_by": "Priya",
              **({"groups": groups} if groups else {})},
        files={"file": ("app.zip", b"not really a zip", "application/zip")},
    )


def test_a_new_app_lands_in_the_groups_chosen_at_deploy(client):
    upload(client, "sales-dashboard", ["Dashboards", "Tools"])

    app_id = int(db.get_app_by_name("sales-dashboard")["id"])
    assert db.app_groups(app_id) == ["Dashboards", "Tools"]


def test_deploying_with_no_group_chosen_is_allowed(client):
    upload(client, "loose-app")

    assert db.app_groups(int(db.get_app_by_name("loose-app")["id"])) == []


def test_updating_an_app_does_not_wipe_the_groups_it_already_had(client):
    """Uploading the same name again is how an app is updated, and that form
    has nothing ticked. Treating blank as "remove them all" would quietly
    unfile an app every time somebody shipped a new version of it."""
    app_id = make("sales-dashboard", ["Dashboards"])

    upload(client, "sales-dashboard")

    assert db.app_groups(app_id) == ["Dashboards"]


def test_but_updating_with_groups_chosen_does_change_them(client):
    app_id = make("sales-dashboard", ["Dashboards"])

    upload(client, "sales-dashboard", ["Tools"])

    assert db.app_groups(app_id) == ["Tools"]
