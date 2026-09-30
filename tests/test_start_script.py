"""The start script, read as text.

It is the one part of the launcher that runs before Python does, so nothing
here can be covered by importing a module. What is checked is the handful of
things that have actually gone wrong on the server.
"""
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "start-launcher.cmd"
TEXT = SCRIPT.read_text(encoding="utf-8")


def line_of(needle: str) -> int:
    for number, line in enumerate(TEXT.splitlines()):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} is not in {SCRIPT.name}")


def test_a_folder_with_no_environment_is_named_not_guessed():
    """cmd.exe reports a missing .venv as "The system cannot find the path
    specified." and nothing else - no path, no cause, no next step. Whoever
    extracted the ZIP into a fresh folder cannot act on that."""
    assert '.venv\\Scripts\\python.exe" (' in TEXT
    assert "install-windows.cmd" in TEXT, "it must say what to run"
    assert "%CD%" in TEXT, "and which folder is not set up"


def test_the_check_comes_before_the_banner():
    """Otherwise the window says the launcher is starting and then exits,
    which reads as a crash rather than as a folder that was never set up."""
    assert line_of('if not exist ".venv') < line_of("App Launcher starting")


def test_a_half_extracted_zip_is_reported_too():
    assert 'if not exist "%~dp0settings.example.cmd" (' in TEXT


@pytest.mark.parametrize("guard", [
    'if not exist ".venv\\Scripts\\python.exe" (',
    'if not exist "%~dp0settings.example.cmd" (',
])
def test_every_guard_stops_rather_than_carrying_on(guard):
    """A guard that prints and falls through to uvicorn helps nobody."""
    rest = TEXT.split(guard, 1)[1].split(")", 1)[0]
    assert "exit /b 1" in rest


def test_settings_are_loaded_before_the_port_is_used():
    assert line_of('call "%~dp0settings.cmd"') < line_of("LAUNCHER_PORT%/")


def test_the_users_own_settings_file_is_never_overwritten():
    """An update replaces the template; settings.cmd holds the real values."""
    assert 'if not exist "%~dp0settings.cmd" (' in TEXT
    assert 'copy /y "%~dp0settings.example.cmd" "%~dp0settings.cmd"' in TEXT
