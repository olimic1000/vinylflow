#!/usr/bin/env python3
"""
VinylFlow Desktop Launcher

Runs VinylFlow in a no-Docker local desktop mode:
- Uses writable user directories for config/temp/output
- Starts the FastAPI backend locally
- Opens the browser automatically
"""

import os
import sys
import webbrowser
import threading
from pathlib import Path

import uvicorn


APP_NAME = "VinylFlow"


def _macos_app_support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / APP_NAME


def _bundled_ffmpeg_path() -> Path | None:
    candidates = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "ffmpeg_bin" / "ffmpeg")

    executable_path = Path(sys.executable).resolve()
    candidates.append(executable_path.parent.parent / "Resources" / "ffmpeg_bin" / "ffmpeg")

    for path in candidates:
        if path.exists() and path.is_file() and os.access(path, os.X_OK):
            return path

    return None


def configure_desktop_environment() -> tuple[str, int]:
    app_data_dir = _macos_app_support_dir()
    config_dir = app_data_dir / "config"
    upload_dir = app_data_dir / "temp_uploads"
    output_dir = Path.home() / "Music" / APP_NAME

    config_dir.mkdir(parents=True, exist_ok=True)
    upload_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("VINYLFLOW_CONFIG_DIR", str(config_dir))
    os.environ.setdefault("VINYLFLOW_UPLOAD_DIR", str(upload_dir))
    os.environ.setdefault("DEFAULT_OUTPUT_DIR", str(output_dir))
    os.environ.setdefault("HOST", "127.0.0.1")
    os.environ.setdefault("PORT", "8000")
    os.environ.setdefault("AUTO_OPEN_BROWSER", "1")

    bundled_ffmpeg = _bundled_ffmpeg_path()
    if bundled_ffmpeg:
        os.environ.setdefault("VINYLFLOW_FFMPEG_PATH", str(bundled_ffmpeg))

    host = os.environ["HOST"]
    port = int(os.environ["PORT"])
    return host, port


def maybe_open_browser(host: str, port: int) -> None:
    if os.getenv("AUTO_OPEN_BROWSER", "1") != "1":
        return

    url = f"http://{host}:{port}"

    def _open() -> None:
        webbrowser.open(url)

    timer = threading.Timer(1.2, _open)
    timer.daemon = True
    timer.start()


def main() -> None:
    host, port = configure_desktop_environment()
    from backend.api import app

    maybe_open_browser(host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
