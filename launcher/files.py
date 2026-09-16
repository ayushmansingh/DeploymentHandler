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


# Stop widening the tail window past this, however few newlines were found.
MAX_TAIL_BYTES = 4 * 1024 * 1024


def tail_text(path: Path, lines: int, encoding: str = "utf-8") -> str:
    """The last `lines` lines of a file, without reading the whole thing.

    A running app appends to its log forever, so reading it all in to keep the
    last few hundred lines would pull tens of megabytes into memory every time
    somebody opened the page - on a server that is already short of it. Seek
    to a window at the end instead and widen it only if that window did not
    hold enough lines.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()

        window = 8192
        while True:
            start = max(0, size - window)
            handle.seek(start)
            chunk = handle.read(size - start)
            # A window that starts mid-line would show a fragment as the first
            # line, so drop everything before the first newline unless we are
            # genuinely at the start of the file.
            text = chunk.decode(encoding, errors="replace")
            if start > 0:
                _, _, text = text.partition("\n")
            found = text.splitlines()
            if len(found) > lines or start == 0 or window >= MAX_TAIL_BYTES:
                return "\n".join(found[-lines:])
            window *= 4


def rotate_if_large(path: Path, limit: int) -> bool:
    """Roll a log over once it passes `limit`, keeping one previous file.

    Called while the app is stopped, so nothing holds the file open - which is
    what makes this safe on Windows, where a rename fails against an open
    handle.
    """
    try:
        if path.stat().st_size <= limit:
            return False
    except OSError:
        return False

    previous = path.with_name(path.name + ".1")
    try:
        if previous.exists():
            previous.unlink()
        path.rename(previous)
        return True
    except OSError:
        # A log we cannot roll is not a reason to refuse to start the app.
        return False
