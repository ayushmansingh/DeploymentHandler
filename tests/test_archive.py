import zipfile
from pathlib import Path

import pytest

from launcher import archive


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def test_extracts_plain_project(tmp_path):
    src = make_zip(tmp_path / "a.zip", {
        "backend/main.py": "app = 1",
        "frontend/package.json": "{}",
    })
    result = archive.extract(src, tmp_path / "out")
    assert (result.root / "backend" / "main.py").is_file()
    assert result.unwrapped_from is None


def test_unwraps_single_wrapper_directory(tmp_path):
    src = make_zip(tmp_path / "a.zip", {
        "my-app/backend/main.py": "app = 1",
        "my-app/frontend/package.json": "{}",
    })
    result = archive.extract(src, tmp_path / "out")
    assert result.unwrapped_from == "my-app"
    assert (result.root / "backend" / "main.py").is_file()
    assert not (result.root / "my-app").exists()


def test_strips_junk_directories(tmp_path):
    src = make_zip(tmp_path / "a.zip", {
        "backend/main.py": "app = 1",
        "frontend/node_modules/react/index.js": "x",
        "backend/__pycache__/main.cpython-311.pyc": "x",
    })
    result = archive.extract(src, tmp_path / "out")
    assert "node_modules" in result.stripped
    assert "__pycache__" in result.stripped
    assert not (result.root / "frontend" / "node_modules").exists()


def test_rejects_path_traversal(tmp_path):
    src = make_zip(tmp_path / "evil.zip", {"../../etc/passwd": "pwned"})
    with pytest.raises(archive.ArchiveError, match="unsafe file path"):
        archive.extract(src, tmp_path / "out")


def test_rejects_absolute_paths(tmp_path):
    src = make_zip(tmp_path / "evil.zip", {"/etc/passwd": "pwned"})
    with pytest.raises(archive.ArchiveError, match="unsafe file path"):
        archive.extract(src, tmp_path / "out")


def test_rejects_non_zip(tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_text("hello")
    with pytest.raises(archive.ArchiveError, match="not a ZIP"):
        archive.extract(plain, tmp_path / "out")


def test_rejects_too_many_entries(tmp_path, monkeypatch):
    from launcher import config
    monkeypatch.setattr(config, "MAX_ARCHIVE_ENTRIES", 3)
    src = make_zip(tmp_path / "big.zip", {f"f{i}.py": "x" for i in range(10)})
    with pytest.raises(archive.ArchiveError, match="far more than a normal project"):
        archive.extract(src, tmp_path / "out")


def test_rejects_oversized_contents(tmp_path, monkeypatch):
    from launcher import config
    monkeypatch.setattr(config, "MAX_EXTRACTED_BYTES", 100)
    src = make_zip(tmp_path / "big.zip", {"data.csv": "x" * 500})
    with pytest.raises(archive.ArchiveError, match="too large"):
        archive.extract(src, tmp_path / "out")


def test_wrapper_named_same_as_child(tmp_path):
    """The unwrap must not clobber a child that shares the wrapper's name."""
    src = make_zip(tmp_path / "a.zip", {
        "app/app/main.py": "x = 1",
        "app/requirements.txt": "fastapi",
    })
    result = archive.extract(src, tmp_path / "out")
    assert result.unwrapped_from == "app"
    assert (result.root / "app" / "main.py").is_file()
    assert (result.root / "requirements.txt").is_file()
