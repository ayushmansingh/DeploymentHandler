"""ZIP intake: validate, extract safely, and normalise the layout.

The guiding assumption is that every upload is slightly wrong. Where a problem
is mechanical (a wrapper folder, a bundled node_modules) we fix it silently;
where it needs a human we raise ArchiveError with a message a non-technical
person can act on.
"""
from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config


class ArchiveError(Exception):
    """A problem with the upload that the uploader has to fix."""


@dataclass
class ExtractResult:
    root: Path
    stripped: list[str] = field(default_factory=list)
    unwrapped_from: str | None = None


def _is_junk(parts: tuple[str, ...]) -> bool:
    return any(p in config.JUNK_DIRS for p in parts)


def _safe_members(zf: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], list[str]]:
    """Filter archive members to those we are willing to write to disk.

    Rejects path traversal ("zip slip") and absolute paths outright; silently
    drops junk directories, symlinks and other non-regular entries.
    """
    infos = zf.infolist()
    if len(infos) > config.MAX_ARCHIVE_ENTRIES:
        raise ArchiveError(
            f"This ZIP contains {len(infos):,} files, which is far more than a "
            "normal project. It probably includes a node_modules or .venv "
            "folder — please zip only your source code."
        )

    keep: list[zipfile.ZipInfo] = []
    stripped: set[str] = set()
    total = 0

    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.endswith("/"):
            continue  # directories are created implicitly

        parts = tuple(p for p in name.split("/") if p not in ("", "."))
        if not parts:
            continue
        if ".." in parts or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
            raise ArchiveError(
                "This ZIP contains an unsafe file path and was rejected. "
                "Please re-create it from your project folder."
            )
        if _is_junk(parts):
            stripped.add(next(p for p in parts if p in config.JUNK_DIRS))
            continue
        # Skip symlinks and anything that is not a regular file.
        if (info.external_attr >> 16) & 0o170000 not in (0, 0o100000):
            continue

        total += info.file_size
        if total > config.MAX_EXTRACTED_BYTES:
            raise ArchiveError(
                "This project is too large once unpacked "
                f"(over {config.MAX_EXTRACTED_BYTES // (1024*1024)} MB). "
                "Please remove large data files or build output before zipping."
            )
        keep.append(info)

    if not keep:
        raise ArchiveError("This ZIP has no usable files in it.")
    return keep, sorted(stripped)


def _unwrap(root: Path) -> str | None:
    """Collapse a single wrapper directory: ZIP -> my-app/ -> the real project.

    Extremely common when someone zips a folder rather than its contents.
    """
    entries = [p for p in root.iterdir() if p.name != "__MACOSX"]
    if len(entries) != 1 or not entries[0].is_dir():
        return None

    inner = entries[0]
    # Move to a temporary name first: a child may share the wrapper's name.
    staging = root.parent / (root.name + ".unwrap")
    if staging.exists():
        shutil.rmtree(staging)
    inner.rename(staging)
    shutil.rmtree(root)
    staging.rename(root)
    return inner.name


def extract(zip_path: Path, dest: Path) -> ExtractResult:
    """Extract `zip_path` into a clean `dest`, normalising as we go."""
    if not zipfile.is_zipfile(zip_path):
        raise ArchiveError(
            "That file is not a ZIP archive. If your AI assistant gave you "
            "loose files, select them all and upload them together instead."
        )

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ArchiveError(
                f"This ZIP appears to be corrupted (bad entry: {bad}). "
                "Please download it again and re-upload."
            )
        members, stripped = _safe_members(zf)
        for info in members:
            target = dest / info.filename.replace("\\", "/")
            # Final guard: the resolved path must stay inside dest.
            if not target.resolve().is_relative_to(dest.resolve()):
                raise ArchiveError("This ZIP contains an unsafe file path and was rejected.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)

    unwrapped = _unwrap(dest)
    return ExtractResult(root=dest, stripped=stripped, unwrapped_from=unwrapped)
