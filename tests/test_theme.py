"""The light/dark switch.

It is all template, so this reads the markup. What is pinned here is the
handful of things that are invisible when right and obvious when wrong: no
flash on load, no dead control without JavaScript, and a dark theme that the
browser's own widgets follow rather than ignore.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from launcher import config, db

BASE = Path(__file__).resolve().parents[1] / "launcher" / "templates" / "base.html"
TEXT = BASE.read_text(encoding="utf-8")

HEAD = TEXT[: TEXT.index("</head>")]
BODY = TEXT[TEXT.index("</head>"):]


@pytest.fixture
def client(data_dir, monkeypatch):
    from launcher import app as app_module

    monkeypatch.setattr(config, "PASSWORD", "")
    db.init()
    return TestClient(app_module.app)


def test_the_theme_is_decided_before_anything_is_drawn():
    """Deciding it later is what makes a dark page flash white on its way in,
    on every single navigation."""
    assert "launcher-theme" in HEAD, "the choice is read in the head"
    assert "launcher-theme" not in BODY.split("<script>")[0]


def test_the_stored_choice_beats_the_machines_setting():
    assert 'choice === "dark" || choice === "light"' in HEAD
    assert "prefers-color-scheme: dark" in HEAD, "and is the fallback, not the rule"


def test_storage_that_throws_cannot_stop_the_page_rendering():
    """A private window or a locked-down policy makes localStorage throw
    rather than return null."""
    reader = HEAD[HEAD.index("function stored()"):HEAD.index("function apply()")]
    assert "try {" in reader and "catch" in reader


def test_the_machine_is_followed_while_nobody_has_chosen():
    """So a laptop that turns dark in the evening takes the dashboard with
    it, rather than needing the switch pressed once a day."""
    assert 'watch.addEventListener("change", onChange)' in HEAD
    assert "if (!stored()) apply();" in HEAD


def test_the_switch_is_hidden_until_the_script_can_run():
    """Without JavaScript it could not remember a choice, and a button that
    forgets is worse than no button."""
    assert 'id="theme-toggle"' in TEXT and "hidden" in TEXT
    assert "toggle.hidden = false;" in TEXT


def test_the_switch_says_which_way_it_goes():
    assert '"Switch to light mode"' in TEXT and '"Switch to dark mode"' in TEXT
    assert 'aria-label="Switch between light and dark"' in TEXT


def test_it_shows_the_theme_you_would_move_to():
    """A moon while it is light. Showing the current theme instead reads as
    a label, and people press it expecting nothing to happen."""
    assert ".themetoggle .sun { display: none; }" in TEXT
    assert ':root[data-theme="dark"] .themetoggle .sun { display: block; }' in TEXT
    assert ':root[data-theme="dark"] .themetoggle .moon { display: none; }' in TEXT


# ---------------------------------------------------------------------------
# The tokens underneath it
# ---------------------------------------------------------------------------

def test_both_themes_live_in_one_block():
    """Two blocks thirty lines apart is how a colour gets changed in one
    theme and not the other."""
    assert TEXT.count(":root {") == 1
    assert len(re.findall(r"light-dark\(", TEXT)) > 10


def test_an_explicit_choice_overrides_the_machine():
    assert ':root[data-theme="light"] { color-scheme: light; }' in TEXT
    assert ':root[data-theme="dark"]  { color-scheme: dark;' in TEXT


def test_the_theme_is_carried_by_color_scheme_not_only_by_colours():
    """color-scheme is what makes the browser's own widgets - text boxes, the
    file picker, checkboxes, scrollbars - follow. Swapping custom properties
    alone leaves a dark page studded with white controls."""
    assert "color-scheme: light dark;" in TEXT


def test_white_on_the_accent_is_not_assumed(client):
    """The dark theme's accent is light enough that white text on it falls
    under 3:1, so anything sitting on the accent takes --accent-ink."""
    assert "color: #fff; }" not in TEXT
    assert "color: var(--accent-ink); }" in TEXT


def test_every_page_carries_the_switch(client):
    for path in ("/", "/deploy", "/admin/update"):
        assert 'id="theme-toggle"' in client.get(path).text, path


def test_the_page_shown_while_the_launcher_restarts_follows_too():
    """It cannot extend the template - the launcher it belongs to is being
    replaced - so it would be the one white flash left in a dark session."""
    from launcher.app import _restarting_page

    page = _restarting_page(8080)
    assert "color-scheme: light dark" in page
    assert "#f9f9f7" in page and "#0d0d0d" in page
