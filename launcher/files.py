"""Filesystem helpers that behave on Windows.

`shutil.rmtree` is not reliable there for directories we have just finished
using: pip marks some installed files read-only, and a process that has only
just exited can still hold a handle for a moment. Both raise PermissionError,
and passing ignore_errors=True hides the failure while leaving the directory
behind - so an app the user deleted keeps its files, and the next app with the
same name inherits them.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import time
from pathlib import Path

ATTEMPTS = 4
PAUSE_SECONDS = 0.4


def _clear_readonly(func, path, _exc_info):
    """Retry a failed removal after dropping the read-only attribute."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_tree(path: Path) -> bool:
    """Delete a directory tree. Returns True if it is gone afterwards.

    Retries briefly: on Windows a handle held by a process that has just been
    stopped clears on its own within a moment.
    """
    if not path.exists():
        return True

    handler = (
        {"onexc": _clear_readonly}
        if sys.version_info >= (3, 12)
        else {"onerror": _clear_readonly}
    )

    for attempt in range(ATTEMPTS):
        try:
            shutil.rmtree(path, **handler)
        except OSError:
            pass
        if not path.exists():
            return True
        if attempt < ATTEMPTS - 1:
            time.sleep(PAUSE_SECONDS)

    return not path.exists()
