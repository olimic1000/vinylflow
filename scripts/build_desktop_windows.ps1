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

python -m PyInstaller `
  --noconfirm `
  --windowed `
  --name VinylFlow `
  --hidden-import backend.api `
  --add-binary "$ffmpegPath;ffmpeg_bin" `
  --add-data "backend/static;backend/static" `
  --add-data "config;config" `
  desktop_launcher.py

Write-Host "Build complete: dist/VinylFlow/VinylFlow.exe"
