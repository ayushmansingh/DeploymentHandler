import pytest

from launcher import naming


@pytest.mark.parametrize("raw,expected", [
    ("Sales Dashboard", "sales-dashboard"),
    ("  My_App  ", "my-app"),
    ("Q3--Report!!", "q3-report"),
    ("inventory", "inventory"),
])
def test_normalises_human_input(raw, expected):
    assert naming.normalise(raw) == expected


@pytest.mark.parametrize("raw", ["ab", "!!", "   "])
def test_rejects_too_short(raw):
    with pytest.raises(naming.InvalidName, match="at least 3 letters"):
        naming.normalise(raw)


def test_rejects_reserved_names():
    with pytest.raises(naming.InvalidName, match="reserved"):
        naming.normalise("API")


def test_truncates_long_names():
    assert len(naming.normalise("x" * 100)) == naming.MAX_LENGTH
