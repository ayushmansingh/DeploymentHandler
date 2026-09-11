"""Per-app configuration: the answer to "my app needs an API key".

Values are write-only. They are typed into the dashboard, stored, and handed
to the app in its environment - but never rendered back, so opening the page
shows that a key exists and when it changed, not what it is. Changing one
means replacing it.

That is a deliberate limit rather than real secrecy: the values sit in
plaintext in the launcher's database, and any app running here can read its
own environment. It stops a token being read off a screen by the next person
to open the page; it is not a secret store.
"""
from __future__ import annotations

import re

# What the value looks like once it has been saved.
MASK = "*******"

# Set by the launcher itself. Letting an app override these would either break
# it (a wrong PORT) or point it away from its own saved files.
RESERVED = {"PORT", "APP_DATA_DIR", "PYTHONUNBUFFERED"}

# Environment variable names as every shell and language agrees on them.
KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

MAX_VALUE_LENGTH = 8192


class InvalidSetting(ValueError):
    """The key or value cannot be used."""


def clean_key(raw: str) -> str:
    """Validate a key, leaving the name exactly as it was typed.

    Names are not upper-cased: an app reading os.environ["Api_Key"] would stop
    finding it, and the person setting it has no way to know why.
    """
    key = raw.strip()
    if not key:
        raise InvalidSetting("Give the setting a name, for example REDASH_API_KEY.")
    if not KEY_PATTERN.match(key):
        raise InvalidSetting(
            f'"{key}" is not a usable name. Use letters, numbers and underscores, '
            "starting with a letter - for example REDASH_API_KEY."
        )
    if key in RESERVED:
        raise InvalidSetting(
            f'"{key}" is set by the server itself and cannot be changed here.'
        )
    return key


def clean_value(raw: str) -> str:
    """Validate a value. Whitespace at the ends is stripped, which is almost
    always a copy-and-paste artefact rather than part of a token."""
    value = raw.strip()
    if not value:
        raise InvalidSetting("Give the setting a value.")
    if len(value) > MAX_VALUE_LENGTH:
        raise InvalidSetting(
            f"That value is too long (limit {MAX_VALUE_LENGTH:,} characters)."
        )
    return value


def environment(stored: dict[str, str], reserved: dict[str, str]) -> dict[str, str]:
    """Merge app settings with the launcher's own variables.

    Reserved names are applied last so a stored setting can never displace
    them, whatever was typed in.
    """
    merged = {k: v for k, v in stored.items() if k not in RESERVED}
    merged.update(reserved)
    return merged
