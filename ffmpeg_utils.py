"""FFmpeg path resolution helpers for source and packaged desktop runs."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _is_executable_file(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.X_OK)


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []

    env_path = os.getenv("VINYLFLOW_FFMPEG_PATH")
    if env_path:
        candidates.append(Path(env_path))

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "ffmpeg_bin" / "ffmpeg")

    executable_path = Path(sys.executable).resolve()
    candidates.append(executable_path.parent.parent / "Resources" / "ffmpeg_bin" / "ffmpeg")

    return candidates


def get_ffmpeg_binary() -> str:
    """Return FFmpeg executable path, preferring bundled binary when available."""
    for candidate in _candidate_paths():
        if _is_executable_file(candidate):
            return str(candidate)

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    return "ffmpeg"


def get_ffmpeg_diagnostics() -> dict:
    """Return FFmpeg runtime diagnostics for support and troubleshooting."""
    resolved = get_ffmpeg_binary()
    resolved_path = Path(resolved)

    source = "unresolved"
    if resolved_path.exists() and "ffmpeg_bin" in resolved_path.parts:
        source = "bundled"
    elif resolved != "ffmpeg":
        source = "system"

    version = None
    executable = False
    error = None

    try:
        result = subprocess.run([resolved, "-version"], capture_output=True, text=True, timeout=5)
        executable = result.returncode == 0
        version_line = result.stdout.splitlines()[0].strip() if result.stdout else ""
        version = version_line or None
        if result.returncode != 0:
            error = (result.stderr or "ffmpeg returned a non-zero exit code").strip()
    except Exception as exc:
        error = str(exc)

    return {
        "path": resolved,
        "source": source,
        "exists": resolved_path.exists() if resolved != "ffmpeg" else bool(shutil.which("ffmpeg")),
        "executable": executable,
        "version": version,
        "error": error,
    }
