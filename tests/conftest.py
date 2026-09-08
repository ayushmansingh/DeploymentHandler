import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def data_dir(monkeypatch):
    """Point the launcher's config at a throwaway directory for each test."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        from launcher import config

        monkeypatch.setattr(config, "DATA_DIR", root)
        monkeypatch.setattr(config, "UPLOAD_DIR", root / "uploads")
        monkeypatch.setattr(config, "SRC_DIR", root / "src")
        monkeypatch.setattr(config, "LOG_DIR", root / "logs")
        monkeypatch.setattr(config, "DB_PATH", root / "launcher.db")
        config.ensure_dirs()
        yield root
