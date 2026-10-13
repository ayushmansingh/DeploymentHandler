"""Counting how often apps get opened, and the Stats tab built from it.

Two things are being protected here. That the count is right - an open is a
person arriving, not every file their page then pulls, and it is counted
whether they came from a bookmark or from the dashboard. And that nothing
about the person is kept, because the promise made on the page is that the
numbers are all there is.
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from launcher import charts, config, db, visits

TODAY = date(2026, 10, 13)


def day(offset: int = 0) -> str:
    return (TODAY - timedelta(days=offset)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# What counts as an open
# ---------------------------------------------------------------------------

def headers(**kw) -> dict:
    return {k.replace("_", "-"): v for k, v in kw.items()}


def test_a_page_a_person_navigated_to_counts():
    assert visits.is_an_open("GET", "/", headers(sec_fetch_mode="navigate"))
    assert visits.is_an_open("GET", "/reports/q3",
                             headers(sec_fetch_mode="navigate"))


@pytest.mark.parametrize("path", ["/assets/app.js", "/api/rows", "/logo.png"])
def test_the_parts_of_a_page_do_not(path):
    """One dashboard pulling sixty files is one open, not sixty-one."""
    assert not visits.is_an_open("GET", path, headers(sec_fetch_mode="no-cors"))


@pytest.mark.parametrize("path,counts", [
    ("/", True), ("/reports", True), ("/api/rows", False),
    ("/assets/app.js", False), ("/style.css", False), ("/f.woff2", False),
    ("/favicon.ico", False), ("/data.json", False),
])
def test_without_the_browser_header_the_path_decides(path, counts):
    """Old browsers, curl and health checks send no Sec-Fetch-Mode."""
    assert visits.is_an_open("GET", path, {}) is counts


def test_posting_data_is_not_opening_an_app():
    assert not visits.is_an_open("POST", "/", headers(sec_fetch_mode="navigate"))


# ---------------------------------------------------------------------------
# Bookmark or dashboard - both count, and the difference is visible
# ---------------------------------------------------------------------------

def test_a_click_from_the_dashboard_is_recognised():
    assert visits.came_from_launcher("http://GN-D-11842N:8080/", 8080)
    assert visits.came_from_launcher("http://172.16.4.30:8080/?group=Tools", 8080)
    assert visits.came_from_launcher("http://localhost:8080/", 8080)


def test_a_bookmark_counts_as_arriving_directly():
    """No Referer at all is the common case, and guessing would be worse."""
    assert not visits.came_from_launcher("", 8080)
    assert not visits.came_from_launcher("http://somewhere-else:9999/", 8080)


def test_a_malformed_referer_is_somebody_elses_problem():
    assert not visits.came_from_launcher("http://[::bad:::/", 8080)


def test_both_kinds_are_counted_as_opens():
    """The point of counting inside the app rather than on the dashboard."""
    for referer in ("", "http://GN-D-11842N:8080/"):
        assert visits.is_an_open("GET", "/", headers(
            sec_fetch_mode="navigate", referer=referer))


# ---------------------------------------------------------------------------
# The counter
# ---------------------------------------------------------------------------

def counter_with_clock(start: float = 1_760_000_000.0):
    written: list[tuple] = []
    now = {"t": start}
    counter = visits.Counter(
        app_id=1, flush_seconds=30,
        writer=lambda *a: written.append(a),
        clock=lambda: now["t"],
    )
    return counter, written, now


def test_counts_are_held_and_written_in_one_go():
    """A write per page view would put the database in the request path."""
    counter, written, _ = counter_with_clock()

    for _ in range(5):
        counter.record()
    assert written == [], "nothing written yet"

    counter.flush()
    assert written == [(1, "2025-10-09", 5, 0, 1_760_000_000.0)]


def test_it_flushes_itself_once_the_timer_is_up():
    counter, written, now = counter_with_clock()
    counter.record()
    now["t"] += 31

    counter.record()

    assert written, "the second request triggered the overdue write"


def test_the_launcher_share_is_kept_apart():
    counter, written, _ = counter_with_clock()

    counter.record(from_launcher=True)
    counter.record(from_launcher=False)
    counter.flush()

    _, _, opens, from_launcher, _ = written[0]
    assert (opens, from_launcher) == (2, 1)


def test_a_flush_that_fails_is_swallowed():
    """A locked database must not turn into a failed page load for somebody
    who is only trying to use the app."""
    def explode(*_):
        raise RuntimeError("database is locked")

    counter = visits.Counter(app_id=1, writer=explode)
    counter.record()

    counter.flush()   # must not raise


def test_a_flush_clears_what_it_wrote():
    counter, written, _ = counter_with_clock()
    counter.record()

    counter.flush()
    counter.flush()

    assert len(written) == 1, "the second flush had nothing left to write"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded(data_dir):
    db.init()
    ids = {}
    for name in ("revenue-explorer", "zip-tool", "old-pilot"):
        app_id = db.create_app(name, owner="Priya")
        db.update_app(app_id, status="live", host_port=24800 + app_id)
        ids[name] = app_id
    for offset in range(0, 40):
        db.record_opens(ids["revenue-explorer"], day(offset), 4, 1)
    for offset in range(0, 5):
        db.record_opens(ids["zip-tool"], day(offset), 1, 0)
    return ids


def test_two_flushes_on_one_day_add_up(data_dir):
    db.init()
    app_id = db.create_app("a")

    db.record_opens(app_id, day(), 3, 1)
    db.record_opens(app_id, day(), 2, 0)

    assert db.opens_by_app(day(1))[app_id] == {"opens": 5, "from_launcher": 1}


def test_nothing_is_written_for_zero_opens(data_dir):
    db.init()
    app_id = db.create_app("a")

    db.record_opens(app_id, day(), 0)

    assert db.opens_by_app(day(1)) == {}


def test_the_last_open_only_ever_moves_forward(data_dir):
    """Flushes can arrive out of order; the newest time has to win."""
    db.init()
    app_id = db.create_app("a")

    db.record_opens(app_id, day(), 1, when=2000.0)
    db.record_opens(app_id, day(), 1, when=1000.0)

    assert db.get_app(app_id)["last_open_at"] == 2000.0


def test_deleting_an_app_takes_its_counts_with_it(seeded):
    db.delete_app(seeded["revenue-explorer"])

    assert seeded["revenue-explorer"] not in db.opens_by_app(day(90))


def test_counts_past_the_window_are_pruned(seeded):
    before = sum(v["opens"] for v in db.opens_by_app(day(90)).values())

    db.prune_visits(day(10))

    after = sum(v["opens"] for v in db.opens_by_app(day(90)).values())
    assert after < before


def test_nothing_about_a_person_is_stored(data_dir):
    """The page promises counts only. This is that promise, as a test."""
    db.init()
    with db.connect() as conn:
        columns = {row["name"].lower()
                   for row in conn.execute("PRAGMA table_info(app_visits)")}

    assert columns == {"app_id", "day", "opens", "from_launcher"}
    for identifying in ("ip", "address", "user", "name", "agent", "session"):
        assert not any(identifying in c for c in columns), identifying


# ---------------------------------------------------------------------------
# Chart geometry
# ---------------------------------------------------------------------------

def test_days_with_no_opens_are_still_points_on_the_chart():
    """A gap in the database is a zero on the chart, not a missing day."""
    days = charts.day_range(5, TODAY)

    assert charts.series_for(days, {day(2): 7}) == [0, 0, 7, 0, 0]


def test_bars_share_one_scale():
    rows = charts.bar_rows([("a", 10), ("b", 5)])
    assert rows[0]["percent"] == 100 and rows[1]["percent"] == 50


def test_a_tiny_count_is_still_a_visible_mark():
    rows = charts.bar_rows([("huge", 1000), ("one", 1)])
    assert rows[1]["percent"] >= 1.5


def test_an_app_with_no_opens_gets_no_bar():
    assert charts.bar_rows([("a", 10), ("b", 0)])[1]["percent"] == 0.0


def test_the_geometry_survives_having_nothing_to_draw():
    assert charts.bar_rows([]) == []
    assert charts.area_path([]) == {}
    assert charts.area_path([0, 0, 0])["top"] == 1, "no division by zero"


def test_weeks_are_whole_weeks():
    days = charts.day_range(14, TODAY)
    weeks = charts.bucket_by_week(days, {d: 1 for d in days})
    assert [w["total"] for w in weeks] == [7, 7]


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

@pytest.fixture
def client(data_dir, monkeypatch):
    from launcher import app as app_module
    monkeypatch.setattr(config, "PASSWORD", "")
    db.init()
    return TestClient(app_module.app)


def test_the_tab_is_there(client):
    assert 'href="/stats"' in client.get("/").text


def test_an_empty_server_says_counting_has_only_just_started(client):
    """Months of real use and a day of counting must not read as "nobody
    uses any of this"."""
    db.create_app("revenue-explorer")

    body = client.get("/stats").text

    assert "No opens counted yet" in body
    assert "Counting started" in body


def test_apps_are_ranked_by_how_much_they_are_opened(client, seeded):
    body = client.get("/stats").text

    ranked = body.index("Most opened")
    assert body.index("revenue-explorer", ranked) < body.index("zip-tool", ranked)


def test_an_app_nobody_opens_is_called_out(client, seeded):
    body = client.get("/stats").text

    assert "Nobody opened these in 30 days" in body
    assert "old-pilot" in body.split("Nobody opened these")[1]


def test_the_week_in_progress_is_left_off(client, seeded):
    """A part-week beside full ones reads as a decline that has not happened."""
    from launcher.app import _stats_context

    weeks = _stats_context(None)["weeks"]

    assert all(
        (date.fromisoformat(w["end"]) - date.fromisoformat(w["start"])).days == 6
        for w in weeks
    )


def test_the_page_says_what_it_does_not_record(client, seeded):
    """Whitespace is collapsed first, so re-wrapping the template later does
    not break a test about what the page promises."""
    import re

    body = re.sub(r"\s+", " ", client.get("/stats").text.lower())

    assert "nothing here records who opened anything" in body
    assert "bookmarks, pasted links and clicks from the dashboard all count" in body


# ---------------------------------------------------------------------------
# The middleware, in the app's own front server
# ---------------------------------------------------------------------------

@pytest.fixture
def front(tmp_path):
    """The real front server, serving a real file, with a counting middleware.

    Driven through the whole stack rather than by calling the classifier,
    because the thing worth proving is that a browser loading a page produces
    exactly one count - and that is a property of the wiring, not the rule.
    """
    from launcher import appserver

    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>app</title>")
    (static / "app.js").write_text("console.log(1)")

    recorded: list[bool] = []

    class Spy:
        def record(self, from_launcher: bool = False) -> None:
            recorded.append(from_launcher)

    app = appserver.build_app(static, None, counter=Spy(), launcher_port=8080)
    client = TestClient(app)
    client.recorded = recorded  # type: ignore[attr-defined]
    return client


def test_loading_a_page_and_its_assets_is_one_open(front):
    front.get("/", headers={"sec-fetch-mode": "navigate"})
    front.get("/app.js", headers={"sec-fetch-mode": "no-cors"})
    front.get("/app.js", headers={"sec-fetch-mode": "no-cors"})

    assert front.recorded == [False], "one arrival, not three requests"


def test_a_bookmarked_visit_counts(front):
    """Nobody went near the dashboard here, and it still has to count."""
    front.get("/", headers={"sec-fetch-mode": "navigate"})

    assert front.recorded == [False]


def test_a_visit_through_the_dashboard_counts_and_is_labelled(front):
    front.get("/", headers={"sec-fetch-mode": "navigate",
                            "referer": "http://GN-D-11842N:8080/?group=Tools"})

    assert front.recorded == [True]


def test_a_deep_link_into_a_single_page_app_counts(front):
    """The file does not exist; the SPA fallback serves index.html. Somebody
    refreshing on /reports is still somebody opening the app."""
    response = front.get("/reports", headers={"sec-fetch-mode": "navigate"})

    assert response.status_code == 200
    assert front.recorded == [False]


def test_a_broken_counter_cannot_break_the_app(tmp_path):
    """Counting is decoration. It is never the reason a page fails to load."""
    from launcher import appserver

    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>app</title>")

    class Broken:
        def record(self, from_launcher: bool = False) -> None:
            raise RuntimeError("the database is on fire")

    client = TestClient(appserver.build_app(static, None, counter=Broken()))

    response = client.get("/", headers={"sec-fetch-mode": "navigate"})

    assert response.status_code == 200
    assert "app" in response.text


def test_an_app_started_without_an_id_is_simply_not_counted(tmp_path):
    """Nothing should change for a front server the launcher did not tell."""
    from launcher import appserver

    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>app</title>")

    client = TestClient(appserver.build_app(static, None))

    assert client.get("/").status_code == 200


def test_the_launcher_tells_the_front_server_which_app_it_is():
    """Without this the front server counts for nobody."""
    import inspect

    from launcher import native, supervisor

    assert '"--app-id"' in inspect.getsource(native.start)
    assert '"--launcher-port"' in inspect.getsource(native.start)
    assert "app_id=int(app_row" in inspect.getsource(supervisor.launch)
