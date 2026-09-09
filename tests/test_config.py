"""Environment checks that prevent unreadable Windows failures."""
import sys

from launcher import config


def test_store_python_is_detected(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    monkeypatch.setattr(
        sys, "executable",
        r"C:\Users\a\AppData\Local\Microsoft\WindowsApps\python3.13.exe",
    )
    assert config.is_store_python() is True

    monkeypatch.setattr(sys, "executable", r"C:\Python313\python.exe")
    assert config.is_store_python() is False


def test_ordinary_python_reports_no_problems(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    monkeypatch.setattr(sys, "executable", r"C:\Python313\python.exe")
    assert config.environment_problems() == []


def test_store_python_under_localappdata_is_a_blocking_problem(monkeypatch, tmp_path):
    """The combination that silently redirects venvs and fails every deploy."""
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    monkeypatch.setattr(
        sys, "executable",
        r"C:\Users\a\AppData\Local\Microsoft\WindowsApps\python3.13.exe",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "Local" / "AppLauncher")

    problems = config.environment_problems()
    assert len(problems) == 1
    assert "every deploy fails" in problems[0]
    assert "LAUNCHER_DATA_DIR" in problems[0]


def test_store_python_outside_localappdata_only_warns(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    monkeypatch.setattr(
        sys, "executable",
        r"C:\Users\a\AppData\Local\Microsoft\WindowsApps\python3.13.exe",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "AppLauncherData")

    problems = config.environment_problems()
    assert len(problems) == 1
    assert "every deploy fails" not in problems[0], "must not block; it works"
    assert "python.org" in problems[0]


def test_default_data_dir_avoids_the_redirected_location(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    monkeypatch.setenv("USERPROFILE", r"C:\Users\a")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\a\AppData\Local")

    default = config._default_data_dir()
    assert "AppData\\Local" not in default
