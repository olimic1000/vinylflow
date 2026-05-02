# -*- mode: python ; coding: utf-8 -*-

import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files


WINDOWS_ICON = 'assets/VinylFlow.ico' if sys.platform.startswith('win') else None
FFMPEG_PATH = shutil.which('ffmpeg')

if not FFMPEG_PATH:
    raise RuntimeError('ffmpeg was not found on PATH. Install ffmpeg before building.')

# certifi CA bundle — needed so requests/discogs_client can verify HTTPS certs.
# Without this, every SSL connection from the packaged app fails with
# CERTIFICATE_VERIFY_FAILED on Windows (and sometimes macOS).
certifi_datas = collect_data_files('certifi')

DATA_FILES = [
    ('backend/static', 'backend/static'),
    *certifi_datas,
]
BINARIES = [(FFMPEG_PATH, 'ffmpeg_bin')]

HIDDEN_IMPORTS = [
    'backend.api',
    'webview',
]

# No modules are excluded — previous exclusion of pythonnet/clr/clr_loader
# was the root cause of the edgechromium backend failing silently and the
# app always falling back to the browser.
EXCLUDES = []

if sys.platform.startswith('win'):
    # Primary backend: edgechromium (WebView2) needs clr / pythonnet at runtime.
    # Fallback backend: qt (pywebview's PySide6 + QtWebEngine), used on Windows
    # installs without the WebView2 Runtime.
    HIDDEN_IMPORTS += [
        'webview.platforms.edgechromium',
        'webview.platforms.qt',
        'clr',
        'clr_loader',
    ]
    # Bundle Python.Runtime.dll so clr_loader can find it inside the bundle.
    # The runtime hook (rthooks/rthook_vinylflow.py) then sets
    # PYTHONNET_RUNTIME_DLL to this path before any imports happen.
    try:
        pythonnet_datas = collect_data_files('pythonnet')
    except Exception:
        pythonnet_datas = []
    DATA_FILES += pythonnet_datas

    # Bundle PySide6 + QtWebEngine so the qt fallback works without the user
    # having to install Qt separately. collect_all gathers Qt's plugins,
    # translations, .pak resources, QtWebEngineProcess.exe, etc.
    try:
        qt_datas, qt_binaries, qt_imports = collect_all('PySide6')
        DATA_FILES += qt_datas
        BINARIES += qt_binaries
        HIDDEN_IMPORTS += qt_imports
    except Exception:
        # PySide6 not installed in the build environment — qt fallback will
        # be unavailable in the resulting bundle, but the build still succeeds.
        pass

elif sys.platform == 'darwin':
    HIDDEN_IMPORTS.append('webview.platforms.cocoa')


a = Analysis(
    ['desktop_launcher.py'],
    pathex=[],
    binaries=BINARIES,
    datas=DATA_FILES,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['rthooks/rthook_vinylflow.py'],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VinylFlow',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=WINDOWS_ICON,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    # UPX can corrupt .NET assemblies, Qt/Chromium binaries, and other
    # third-party executables. ffmpeg.exe in particular can be mis-flagged by
    # AV when UPX-packed; QtWebEngineProcess.exe + Qt6*.dll fail to load when
    # compressed.
    upx_exclude=[
        'Python.Runtime.dll',
        'ffmpeg.exe',
        'QtWebEngineProcess.exe',
        'Qt6Core.dll',
        'Qt6Gui.dll',
        'Qt6Widgets.dll',
        'Qt6Network.dll',
        'Qt6Qml.dll',
        'Qt6WebEngineCore.dll',
        'Qt6WebEngineWidgets.dll',
        'Qt6WebChannel.dll',
        'Qt6Positioning.dll',
        'Qt6PrintSupport.dll',
    ],
    name='VinylFlow',
)
app = BUNDLE(
    coll,
    name='VinylFlow.app',
    icon='assets/VinylFlow.icns',
    bundle_identifier=None,
)
