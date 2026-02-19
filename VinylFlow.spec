# -*- mode: python ; coding: utf-8 -*-

import shutil
import sys
from pathlib import Path


WINDOWS_ICON = 'assets/VinylFlow.ico' if sys.platform.startswith('win') else None
FFMPEG_PATH = shutil.which('ffmpeg')

if not FFMPEG_PATH:
    raise RuntimeError('ffmpeg was not found on PATH. Install ffmpeg before building.')

DATA_FILES = [('backend/static', 'backend/static')]

HIDDEN_IMPORTS = [
    'backend.api',
    'webview',
]

EXCLUDES = []

if sys.platform.startswith('win'):
    HIDDEN_IMPORTS.append('webview.platforms.edgechromium')
    EXCLUDES.extend([
        'pythonnet',
        'clr',
        'clr_loader',
    ])
elif sys.platform == 'darwin':
    HIDDEN_IMPORTS.append('webview.platforms.cocoa')


a = Analysis(
    ['desktop_launcher.py'],
    pathex=[],
    binaries=[(FFMPEG_PATH, 'ffmpeg_bin')],
    datas=DATA_FILES,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
    upx_exclude=[],
    name='VinylFlow',
)
app = BUNDLE(
    coll,
    name='VinylFlow.app',
    icon='assets/VinylFlow.icns',
    bundle_identifier=None,
)
