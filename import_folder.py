"""Import-folder path logic: list, resolve and vet audio files in place.

Pure (no FastAPI): everything that decides *which files may be referenced
in place* lives here so it can be unit-tested and audited in one spot.
Nothing in this module ever writes to, or deletes from, the import dir.
"""

import os
import time
from pathlib import Path
from typing import Optional

from audio_processor import SUPPORTED_INPUT_EXTENSIONS

MAX_DEPTH = 3          # directory levels below the import dir
MAX_FILES = 500
STABLE_AGE_S = 10.0    # younger mtime = probably still being copied


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_INPUT_EXTENSIONS


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def overlap_error(import_dir: Path, upload_dir: Path) -> Optional[str]:
    """The import dir must not equal, contain or sit inside the uploads dir
    (the uploads dir is rmtree'd by the cleanup paths)."""
    imp, up = import_dir.resolve(), upload_dir.resolve()
    if _inside(imp, up) or _inside(up, imp):
        return "Import folder overlaps the uploads folder"
    return None


def dir_error(import_dir: Path, upload_dir: Path) -> Optional[str]:
    """Short error string when the configured import dir is unusable."""
    if not import_dir.is_dir():
        return "Import folder is missing"
    if not os.access(import_dir, os.R_OK | os.X_OK):
        return "Import folder is not readable"
    return overlap_error(import_dir, upload_dir)


def list_audio(import_dir: Path, now: Optional[float] = None) -> list[dict]:
    """Supported audio files under ``import_dir`` as
    ``{name, size, mtime, stable, path}`` (``path`` = resolved Path, for the
    caller's own matching; drop it before sending)."""
    now = time.time() if now is None else now
    root = import_dir.resolve()
    found: list[dict] = []

    def walk(directory: Path, depth: int) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            path = Path(entry.path)
            try:
                real = path.resolve()
                if not _inside(real, root):
                    continue  # symlink pointing outside the import dir
                if entry.is_dir():  # follows symlinks; already vetted above
                    if depth < MAX_DEPTH:
                        walk(path, depth + 1)
                elif entry.is_file() and is_supported(path):
                    st = real.stat()
                    found.append({
                        "name": path.relative_to(import_dir).as_posix(),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "stable": (now - st.st_mtime) >= STABLE_AGE_S,
                        "path": real,
                    })
            except OSError:
                continue

    walk(import_dir, 0)
    found.sort(key=lambda f: f["name"].lower())
    return found[:MAX_FILES]


def resolve_name(import_dir: Path, name: str) -> Path:
    """Resolve a relative ``name`` to a real file inside ``import_dir``.
    Raises ValueError with a user-facing reason."""
    if not isinstance(name, str) or not name.strip() or "\x00" in name:
        raise ValueError("Invalid file name")
    if name.startswith(("/", "\\")) or Path(name).is_absolute() or (len(name) > 1 and name[1] == ":"):
        raise ValueError("Absolute paths are not allowed")
    parts = name.replace("\\", "/").split("/")
    if ".." in parts:
        raise ValueError("Path traversal is not allowed")
    if any(p.startswith(".") for p in parts):
        raise ValueError("Hidden files are not listed")
    root = import_dir.resolve()
    try:
        real = (root / name).resolve()
    except (OSError, RuntimeError):
        raise ValueError("File not found")
    if not _inside(real, root) or real == root:
        raise ValueError("File is outside the import folder")
    return vet_file(real)


def vet_file(real: Path) -> Path:
    """Regular file with a supported extension (path already resolved)."""
    if not real.is_file():
        raise ValueError("File not found")
    if not is_supported(real):
        raise ValueError("Unsupported file type")
    return real


def is_stable(path: Path, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    return (now - path.stat().st_mtime) >= STABLE_AGE_S
