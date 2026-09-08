"""App name normalisation.

The name becomes a Docker container name, an image tag and a directory, so it
has to be restrictive. We normalise rather than reject wherever we can: people
type "Sales Dashboard" and mean "sales-dashboard".
"""
from __future__ import annotations

import re

MAX_LENGTH = 40
_VALID = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")

# Names that would collide with our own routes or confuse the dashboard.
RESERVED = {"api", "static", "app", "apps", "upload", "healthz", "admin", "launcher"}


class InvalidName(ValueError):
    """The supplied app name cannot be used."""


def normalise(raw: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    name = re.sub(r"-{2,}", "-", name)[:MAX_LENGTH].strip("-")

    if len(name) < 3:
        raise InvalidName(
            "Please give your app a name of at least 3 letters, for example "
            "\"sales-dashboard\"."
        )
    if name in RESERVED:
        raise InvalidName(f"\"{name}\" is reserved. Please choose a different name.")
    if not _VALID.match(name):
        raise InvalidName(
            "App names can only use letters, numbers and hyphens, for example "
            "\"sales-dashboard\"."
        )
    return name
