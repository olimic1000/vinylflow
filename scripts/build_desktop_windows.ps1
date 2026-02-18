$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RootDir

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg) {
    Write-Error "ffmpeg not found in PATH. Install ffmpeg first (for example: choco install ffmpeg -y)."
}

python -m pip install -r requirements.txt
python -m pip install pyinstaller

$ffmpegPath = $ffmpeg.Source

$pyiArgs = @(
    "--noconfirm",
    "--windowed",
    "--name", "VinylFlow",
    "--hidden-import", "backend.api",
    "--hidden-import", "webview",
    "--hidden-import", "webview.platforms.winforms",
    "--add-binary", "$ffmpegPath;ffmpeg_bin",
    "--add-data", "backend/static;backend/static"
)

if (Test-Path "config") {
    $pyiArgs += @("--add-data", "config;config")
}

$pyiArgs += "desktop_launcher.py"

python -m PyInstaller @pyiArgs

Write-Host "Build complete: dist/VinylFlow/VinylFlow.exe"
