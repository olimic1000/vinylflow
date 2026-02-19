#!/usr/bin/env python3
"""
VinylFlow Desktop Launcher

Runs VinylFlow in a no-Docker local desktop mode:
- Uses writable user directories for config/temp/output
- Starts the FastAPI backend locally
- Opens a native desktop app window (WebView2 on Windows, WKWebView on macOS)
"""

import os
import sys
import time
import threading
import webbrowser
from socket import create_connection
from pathlib import Path

import uvicorn

# Must be set before `import webview` so pywebview picks up the correct backend.
if sys.platform.startswith("win"):
    os.environ.setdefault("PYWEBVIEW_GUI", "edgechromium")

# Point requests/urllib3 at the bundled certifi CA bundle when running from a
# PyInstaller one-folder bundle.  The runtime hook already does this, but we
# repeat it here in case the launcher is run outside a bundle (development).
def _configure_ssl_certs() -> None:
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    cacert = Path(meipass) / "certifi" / "cacert.pem"
    if cacert.exists():
        os.environ.setdefault("SSL_CERT_FILE", str(cacert))
        os.environ.setdefault("REQUESTS_CA_BUNDLE", str(cacert))

_configure_ssl_certs()

try:
    import webview
    _WEBVIEW_IMPORT_ERROR: Exception | None = None
except Exception as exc:
    webview = None
    _WEBVIEW_IMPORT_ERROR = exc


APP_NAME = "VinylFlow"


class DesktopApi:
    def select_output_folder(self, initial_path: str = "") -> str | None:
        if webview is None:
            return None
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


def _windows_app_support_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / "AppData" / "Roaming" / APP_NAME


def _bundled_ffmpeg_path() -> Path | None:
    """Return path to bundled ffmpeg binary, or None if not found."""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None

    ffmpeg_dir = Path(meipass) / "ffmpeg_bin"
    # On Windows the binary name includes the .exe extension.
    candidates = ["ffmpeg.exe", "ffmpeg"] if sys.platform.startswith("win") else ["ffmpeg"]
    for name in candidates:
        path = ffmpeg_dir / name
        if path.exists() and path.is_file():
            return path
    return None


def _check_webview2_available() -> bool:
    """
    Return True if the Microsoft WebView2 Runtime is installed.
    Only meaningful on Windows; always returns True on other platforms.
    WebView2 is pre-installed on Windows 11.  On Windows 10 it ships with
    Microsoft Edge, but may be absent on fresh / locked-down installs.
    """
    if not sys.platform.startswith("win"):
        return True
    try:
        import winreg
        client_guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        subkeys = [
            rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{client_guid}",
            rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{client_guid}",
        ]
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for subkey in subkeys:
                try:
                    with winreg.OpenKey(hive, subkey):
                        return True
                except OSError:
                    pass
    except Exception:
        pass
    return False


def configure_desktop_environment() -> tuple[str, int]:
    if sys.platform.startswith("win"):
        app_data_dir = _windows_app_support_dir()
    elif sys.platform == "darwin":
        app_data_dir = _macos_app_support_dir()
    else:
        app_data_dir = Path.home() / ".config" / APP_NAME

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


def _open_browser_fallback(app_url: str, server_thread: threading.Thread) -> None:
    """Open app in default browser and keep the server alive."""
    webbrowser.open(app_url)
    while server_thread.is_alive():
        time.sleep(0.5)


def main() -> None:
    host, port = configure_desktop_environment()
    server_thread = threading.Thread(target=lambda: _run_server(host, port), daemon=True)
    server_thread.start()

    if not _wait_for_server(host, port):
        raise RuntimeError(f"VinylFlow backend failed to start on http://{host}:{port}")

    app_url = f"http://{host}:{port}"

    # --- webview not importable at all ---
    if webview is None:
        print(
            f"[VinylFlow] pywebview unavailable ({_WEBVIEW_IMPORT_ERROR}). "
            "Opening in default browser.",
            file=sys.stderr,
        )
        _open_browser_fallback(app_url, server_thread)
        return

    # --- Windows: check WebView2 Runtime before attempting to start ---
    if sys.platform.startswith("win") and not _check_webview2_available():
        print(
            "[VinylFlow] Microsoft WebView2 Runtime not found.\n"
            "  Download: https://developer.microsoft.com/microsoft-edge/webview2/\n"
            "  Opening in default browser as fallback.",
            file=sys.stderr,
        )
        _open_browser_fallback(app_url, server_thread)
        return

    # --- Try native desktop window ---
    try:
        webview.create_window(
            "VinylFlow",
            app_url,
            width=1280,
            height=900,
            min_size=(900, 700),
            js_api=DesktopApi(),
        )
        if sys.platform.startswith("win"):
            webview.start(gui="edgechromium")
        else:
            webview.start()
    except Exception as exc:
        print(
            f"[VinylFlow] Native window failed ({exc}). Opening in default browser.",
            file=sys.stderr,
        )
        _open_browser_fallback(app_url, server_thread)


if __name__ == "__main__":
    main()
