$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RootDir

$AppDir = if ($env:APP_DIR) { $env:APP_DIR } else { "dist/VinylFlow" }
$OutZip = if ($env:OUT_ZIP) { $env:OUT_ZIP } else { "dist/VinylFlow-windows-unsigned.zip" }

if (-not (Test-Path $AppDir)) {
    Write-Error "App directory not found at $AppDir. Build first with: powershell -ExecutionPolicy Bypass -File .\scripts\build_desktop_windows.ps1"
}

$OutDir = Split-Path -Parent $OutZip
if ($OutDir -and -not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir | Out-Null
}

if (Test-Path $OutZip) {
    Remove-Item $OutZip -Force
}

Write-Host "Packaging unsigned Windows app..."
Compress-Archive -Path "$AppDir\*" -DestinationPath $OutZip -CompressionLevel Optimal

Write-Host "Created: $OutZip"
