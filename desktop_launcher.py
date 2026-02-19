#!/usr/bin/env python3
"""
VinylFlow Desktop Launcher

Runs VinylFlow in a no-Docker local desktop mode:
- Uses writable user directories for config/temp/output
- Starts the FastAPI backend locally
- Opens a native desktop app window
"""

import os
import sys
import time
import threading
from socket import create_connection
from pathlib import Path

import uvicorn
import webview


APP_NAME = "VinylFlow"


class DesktopApi:
    def select_output_folder(self, initial_path: str = "") -> str | None:
        try:
            directory = None
            if initial_path:
                candidate = Path(initial_path).expanduser()
                if candidate.exists() and candidate.is_dir():
                    directory = str(candidate)

            window = webview.windows[0]
            dialog_type = webview.FOLDER_DIALOG
            if hasattr(webview, "FileDialog") and hasattr(webview.FileDialog, "FOLDER"):
                dialog_type = webview.FileDialog.FOLDER

            result = window.create_file_dialog(dialog_type, directory=directory)
            if not result:
                return None

            return str(result[0])
        except Exception:
            return None


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
    os.environ.setdefault("AUTO_OPEN_BROWSER", "0")

    bundled_ffmpeg = _bundled_ffmpeg_path()
    if bundled_ffmpeg:
        bundled_ffmpeg_str = str(bundled_ffmpeg)
        os.environ.setdefault("VINYLFLOW_FFMPEG_PATH", bundled_ffmpeg_str)
        os.environ.setdefault("FFMPEG_BINARY", bundled_ffmpeg_str)
        os.environ.setdefault("IMAGEIO_FFMPEG_EXE", bundled_ffmpeg_str)

        ffmpeg_dir = str(bundled_ffmpeg.parent)
        current_path = os.environ.get("PATH", "")
        path_parts = [p for p in current_path.split(os.pathsep) if p]
        if ffmpeg_dir not in path_parts:
            os.environ["PATH"] = os.pathsep.join([ffmpeg_dir, *path_parts])

    host = os.environ["HOST"]
    port = int(os.environ["PORT"])
    return host, port


def _run_server(host: str, port: int) -> None:
    from backend.api import app

    uvicorn.run(app, host=host, port=port)


def _wait_for_server(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> None:
    host, port = configure_desktop_environment()
    server_thread = threading.Thread(target=lambda: _run_server(host, port), daemon=True)
    server_thread.start()

    if not _wait_for_server(host, port):
        raise RuntimeError(f"VinylFlow backend failed to start on http://{host}:{port}")

    webview.create_window(
        "VinylFlow",
        f"http://{host}:{port}",
        width=1280,
        height=900,
        min_size=(900, 700),
        js_api=DesktopApi(),
    )
    if sys.platform.startswith("win"):
        webview.start(gui="edgechromium")
    else:
        webview.start()


if __name__ == "__main__":
    main()
