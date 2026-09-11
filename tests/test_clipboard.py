"""Copying to the clipboard from a plain HTTP address.

The launcher is reached over http:// at a LAN address, which browsers do not
treat as a secure context. navigator.clipboard does not exist there, so any
page calling it directly has a copy button that silently does nothing on every
machine except the server itself - which is exactly why this went unnoticed:
testing against localhost, a secure context, hides it.
"""
import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "launcher" / "templates"
BASE = TEMPLATES / "base.html"


def strip_comments(js: str) -> str:
    return "\n".join(
        line for line in js.splitlines() if not line.strip().startswith("//")
    )


def test_the_shared_helper_guards_the_clipboard_api():
    code = strip_comments(BASE.read_text())
    assert "window.isSecureContext && navigator.clipboard" in code, (
        "navigator.clipboard must be feature-detected, not assumed"
    )


def test_the_shared_helper_has_a_fallback_that_works_without_it():
    code = strip_comments(BASE.read_text())
    assert 'document.execCommand("copy")' in code, (
        "insecure origins need the execCommand path; without it copying is dead"
    )


@pytest.mark.parametrize("template", ["deploy.html", "detail.html"])
def test_pages_never_touch_the_clipboard_api_directly(template):
    """Pages must go through the helper, which handles the insecure case."""
    code = strip_comments((TEMPLATES / template).read_text())
    assert "navigator.clipboard" not in code, (
        f"{template} calls navigator.clipboard directly, which throws on http://"
    )
    assert "copyWithFeedback" in code


@pytest.mark.parametrize("template", ["deploy.html", "detail.html"])
def test_copy_buttons_report_failure_rather_than_going_quiet(template):
    code = (TEMPLATES / template).read_text()
    assert "copyWithFeedback" in code
    # The helper says so on failure; the page reacts by revealing the text.
    assert re.search(r"if \(!copied\)", code), (
        f"{template} should show the text when copying is unavailable"
    )
