"""Updating the launcher from a browser.

This is the one upload that can make the server unreachable, so the checks
that run before anything is replaced matter more than the happy path.
"""
import io
import zipfile
from pathlib import Path

import pytest

from launcher import config, selfupdate

REAL = Path(__file__).resolve().parents[1]


def launcher_zip(tmp_path: Path, overrides: dict[str, bytes] | None = None,
                 omit: tuple[str, ...] = (), wrap: str = "") -> Path:
    """A ZIP of the real launcher, optionally damaged in a specific way."""
    files = {
        name: (REAL / name).read_bytes()
        for name in ("launcher/app.py", "launcher/deployer.py",
                     "launcher/config.py", "requirements.txt")
    }
    files.update(overrides or {})
    for name in omit:
        files.pop(name, None)

    path = tmp_path / "update.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(f"{wrap}/{name}" if wrap else name, content)
    return path


def test_accepts_a_real_launcher_zip(data_dir, tmp_path):
    staged = selfupdate.stage(launcher_zip(tmp_path))
    assert (staged.root / "launcher" / "app.py").is_file()


def test_unwraps_a_zip_of_a_folder(data_dir, tmp_path):
    staged = selfupdate.stage(launcher_zip(tmp_path, wrap="applauncher"))
    assert (staged.root / "launcher" / "app.py").is_file()


def test_rejects_an_application_zip(data_dir, tmp_path):
    """Uploading a dashboard here would replace the launcher with it."""
    path = tmp_path / "app.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("backend/main.py", "app = 1")
        zf.writestr("frontend/package.json", "{}")

    with pytest.raises(selfupdate.UpdateError, match="does not look like the App Launcher"):
        selfupdate.stage(path)


def test_rejects_a_zip_missing_core_files(data_dir, tmp_path):
    with pytest.raises(selfupdate.UpdateError, match="launcher/deployer.py"):
        selfupdate.stage(launcher_zip(tmp_path, omit=("launcher/deployer.py",)))


def test_rejects_python_that_will_not_compile(data_dir, tmp_path):
    """A truncated upload must be caught before anything is replaced."""
    broken = launcher_zip(tmp_path, overrides={"launcher/app.py": b"def broken(:\n"})

    with pytest.raises(selfupdate.UpdateError, match="will not compile"):
        selfupdate.stage(broken)


def test_rejects_path_traversal(data_dir, tmp_path):
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("../../evil.py", "pwned")

    with pytest.raises(selfupdate.UpdateError, match="unsafe path"):
        selfupdate.stage(path)


def test_rejects_a_file_that_is_not_a_zip(data_dir, tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_text("hello")

    with pytest.raises(selfupdate.UpdateError, match="not a ZIP"):
        selfupdate.stage(plain)


def test_notices_when_dependencies_changed(data_dir, tmp_path):
    same = selfupdate.stage(launcher_zip(tmp_path))
    assert same.requirements_changed is False

    changed = selfupdate.stage(launcher_zip(
        tmp_path, overrides={"requirements.txt": b"fastapi==0.1.0\n"}
    ))
    assert changed.requirements_changed is True


@pytest.mark.parametrize("path", [".venv", ".venv/bin/python", "deploy/settings.cmd"])
def test_settings_and_environment_are_never_replaced(path):
    """Losing either would break the install the update is meant to improve."""
    assert selfupdate._is_preserved(path)


@pytest.mark.parametrize("path", ["launcher/app.py", "deploy/start-launcher.cmd",
                                  "README.md", "scripts/selftest.py"])
def test_everything_else_is_replaced(path):
    assert not selfupdate._is_preserved(path)


def test_launch_command_is_recorded_for_the_restart(data_dir, monkeypatch):
    monkeypatch.setenv("LAUNCHER_PUBLIC_HOST", "192.168.1.50")
    monkeypatch.setenv("UNRELATED_SETTING", "ignore me")

    selfupdate.record_launch_command()

    import json
    record = json.loads((config.DATA_DIR / selfupdate.LAUNCH_RECORD).read_text())
    assert record["command"]
    assert record["env"]["LAUNCHER_PUBLIC_HOST"] == "192.168.1.50"
    assert "UNRELATED_SETTING" not in record["env"], "only launcher settings carry over"


def test_module_invocations_keep_their_dash_m_form(monkeypatch):
    """`python -m uvicorn` must not be rebuilt as `python .../__main__.py`.

    Running the file puts uvicorn's own directory first on sys.path, where its
    logging.py shadows the standard library's and the process dies on import -
    so the restart would never come back.
    """
    import sys
    from types import SimpleNamespace

    fake_main = SimpleNamespace(
        __spec__=SimpleNamespace(name="uvicorn.__main__", parent="uvicorn")
    )
    monkeypatch.setitem(sys.modules, "__main__", fake_main)
    monkeypatch.setattr(
        sys, "argv",
        ["/site-packages/uvicorn/__main__.py", "launcher.app:app", "--port", "8080"],
    )

    command = selfupdate.launch_command()

    assert command[1:3] == ["-m", "uvicorn"]
    assert command[3:] == ["launcher.app:app", "--port", "8080"]
    assert not any(part.endswith("__main__.py") for part in command)


def test_script_invocations_are_rebuilt_as_scripts(monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(__spec__=None))
    monkeypatch.setattr(sys, "argv", ["serve.py", "--port", "8080"])

    assert selfupdate.launch_command()[1:] == ["serve.py", "--port", "8080"]
