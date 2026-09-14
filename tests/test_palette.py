"""Colours that carry meaning have to be readable.

The name of whoever deployed an app is small text, so it needs the 4.5:1
contrast ratio rather than the 3:1 a chart mark can live with. Several
plausible-looking choices do not clear that on the light surface, so the
check is here rather than left to the eye.
"""
import re
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1] / "launcher" / "templates" / "base.html"

LIGHT_SURFACE = "#fcfcfb"
DARK_SURFACE = "#1a1a19"
SMALL_TEXT_RATIO = 4.5


def relative_luminance(colour: str) -> float:
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def token(name: str) -> tuple[str, str]:
    """The light and dark values of a CSS custom property."""
    css = BASE.read_text()
    found = re.findall(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", css)
    assert len(found) == 2, f"expected a light and a dark value for --{name}, got {found}"
    return found[0], found[1]


def test_contrast_maths_matches_known_values():
    """Guards the test itself: black on white is 21:1, white on white is 1:1."""
    assert contrast("#000000", "#ffffff") == pytest.approx(21, abs=0.1)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1, abs=0.01)


def test_the_person_colour_is_readable_in_both_themes():
    light, dark = token("person")
    assert contrast(light, LIGHT_SURFACE) >= SMALL_TEXT_RATIO
    assert contrast(dark, DARK_SURFACE) >= SMALL_TEXT_RATIO


def test_the_person_colour_is_not_the_link_colour():
    """A name is not clickable; sharing the accent would suggest it is."""
    assert token("person") != token("accent")


def test_names_are_not_distinguished_by_colour_alone():
    """Weight carries it too, for greyscale and for colour blindness."""
    css = BASE.read_text()
    rule = css[css.index(".who {"):css.index("}", css.index(".who {"))]
    assert "font-weight" in rule
