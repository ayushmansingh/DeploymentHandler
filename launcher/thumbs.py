"""A picture of each app's home page, for the dashboard.

Windows 11 ships Edge, which is Chromium and takes a screenshot from the
command line. So this needs nothing installed, nothing downloaded and no
administrator - the one browser every one of these machines already has is
the one that does the work.

There is always a picture. When no browser can be found, or the capture
fails, or the app has never been live, the card falls back to a tile drawn
from the app's own name - so the dashboard never shows a broken image or a
grey hole where a screenshot should be.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config

# Where a captured picture lives. One per app, replaced on each capture.
THUMB_DIR = config.DATA_DIR / "thumbs"

# Wide enough to show a real layout, small enough that a dozen of them on one
# page stay light. The card crops the bottom, so this is mostly the header.
WIDTH = 1200
HEIGHT = 750

# A page that has not finished drawing photographs as a blank white rectangle,
# so give it a moment. Chromium counts this from when the page loads.
SETTLE_MS = 2500

# Hard ceiling on the whole capture. A browser that hangs must never hold up
# a deploy, and this runs after the app is already live and reported.
TIMEOUT_SECONDS = 40

# Every Chromium-derived browser takes the same flags. Edge is first because
# it is the one present on a stock Windows 11 machine.
_WINDOWS_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

_POSIX_CANDIDATES = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "microsoft-edge", "microsoft-edge-stable",
)


def browser_path() -> str | None:
    """A Chromium-based browser this machine already has, if any."""
    configured = config.SCREENSHOT_BROWSER
    if configured:
        return configured if Path(configured).is_file() else None

    if config.IS_WINDOWS:
        for candidate in _WINDOWS_CANDIDATES:
            if Path(candidate).is_file():
                return candidate
        return shutil.which("msedge") or shutil.which("chrome")

    for name in _POSIX_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _sandbox_flags() -> list[str]:
    """Chromium refuses to run as root with its sandbox on.

    That only happens in a container, which is where this gets tested rather
    than where it runs. On Windows, under a normal account, the sandbox works
    and is left alone - the browser is rendering pages from apps the team
    uploaded, so the isolation is worth keeping wherever it is available.
    """
    if config.IS_WINDOWS:
        return []
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return ["--no-sandbox"]
    return []


def thumb_path(name: str) -> Path:
    return THUMB_DIR / f"{name}.png"


def has_thumb(name: str) -> bool:
    path = thumb_path(name)
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def capture(name: str, url: str) -> bool:
    """Photograph an app's home page. False if we could not.

    Never raises: a dashboard picture is decoration, and nothing about a
    deploy should fail because a browser would not start.
    """
    browser = browser_path()
    if not browser or not url:
        return False

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    target = thumb_path(name)

    # Chromium writes the screenshot to the current directory under some
    # versions, so it is given a private one to work in and the result is
    # moved. A throwaway profile keeps it from touching the real one, which
    # may be open in front of somebody on the server's desktop.
    with tempfile.TemporaryDirectory(prefix="launcher-shot-") as workspace:
        shot = Path(workspace) / "shot.png"
        command = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            # It only ever loads a page served by this same machine.
            "--disable-extensions",
            *_sandbox_flags(),
            f"--user-data-dir={Path(workspace) / 'profile'}",
            f"--window-size={WIDTH},{HEIGHT}",
            f"--virtual-time-budget={SETTLE_MS}",
            f"--screenshot={shot}",
            url,
        ]
        try:
            subprocess.run(
                command, cwd=workspace, timeout=TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False

        if not shot.is_file() or shot.stat().st_size == 0:
            return False
        try:
            shot.replace(target)
        except OSError:
            # Across devices replace() will not work; fall back to a copy.
            try:
                shutil.copyfile(shot, target)
            except OSError:
                return False
    return True


def forget(name: str) -> None:
    """Drop an app's picture, when the app itself is deleted."""
    try:
        thumb_path(name).unlink(missing_ok=True)
    except OSError:
        pass


def tile(name: str) -> dict:
    """A stable colour and monogram for an app with no picture.

    Derived from the name, so an app looks the same on every refresh and two
    apps rarely collide - which is what makes a fallback read as a design
    rather than as a missing image.
    """
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    hue = digest[0] / 255 * 360
    # A second hue near the first keeps the gradient calm rather than gaudy.
    hue_two = (hue + 26 + digest[1] / 255 * 40) % 360
    letters = "".join(part[0] for part in name.replace("_", "-").split("-") if part)
    return {
        "from": f"hsl({hue:.0f} 62% 58%)",
        "to": f"hsl({hue_two:.0f} 58% 44%)",
        "monogram": (letters[:2] or name[:2]).upper(),
    }
