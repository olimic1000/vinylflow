# -*- mode: python ; coding: utf-8 -*-

import shutil
import sys


WINDOWS_ICON = 'assets/VinylFlow.ico' if sys.platform.startswith('win') else None
FFMPEG_PATH = shutil.which('ffmpeg')

if not FFMPEG_PATH:
    raise RuntimeError('ffmpeg was not found on PATH. Install ffmpeg before building.')


a = Analysis(
    ['desktop_launcher.py'],
    pathex=[],
    binaries=[(FFMPEG_PATH, 'ffmpeg_bin')],
    datas=[('backend/static', 'backend/static'), ('config', 'config')],
    hiddenimports=[
        'backend.api',
        'webview',
        'webview.platforms.cocoa',
        'webview.platforms.winforms',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
