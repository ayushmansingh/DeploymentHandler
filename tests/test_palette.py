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
    """The light and dark values of a CSS custom property.

    They are written as `light-dark(light, dark)`, preceded by a plain value
    for browsers that do not know the function. Reading the pair out of the
    stylesheet - rather than listing the colours here - is what keeps this
    check honest when somebody changes one of them.
    """
    css = BASE.read_text()
    pair = re.search(
        rf"--{name}:\s*light-dark\(\s*(#[0-9a-fA-F]{{6}})\s*,\s*(#[0-9a-fA-F]{{6}})\s*\)",
        css,
    )
    assert pair, f"expected a light-dark() pair for --{name}"
    return pair.group(1), pair.group(2)


def test_every_pair_has_a_plain_value_in_front_of_it():
    """An old browser drops a declaration it cannot parse. Without the plain
    value in front, that colour would be lost rather than fall back to the
    light theme."""
    css = BASE.read_text()
    for name in re.findall(r"--([a-z-]+):\s*light-dark\(", css):
        before = css[:css.index(f"--{name}: light-dark(")]
        assert before.rstrip().endswith(";"), name
        assert f"--{name}:" in before.rsplit("\n", 2)[-2], (
            f"--{name} has no plain value before its light-dark() pair"
        )


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
