$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RootDir

$AppDir = if ($env:APP_DIR) { $env:APP_DIR } else { "dist/VinylFlow" }
$AppExe = if ($env:APP_EXE) { $env:APP_EXE } else { "dist/VinylFlow.exe" }
$OutZip = if ($env:OUT_ZIP) { $env:OUT_ZIP } else { "dist/VinylFlow-windows-unsigned.zip" }

if (-not (Test-Path $AppDir) -and -not (Test-Path $AppExe)) {
    Write-Error "App output not found at $AppDir or $AppExe. Build first with: powershell -ExecutionPolicy Bypass -File .\scripts\build_desktop_windows.ps1"
}

$OutDir = Split-Path -Parent $OutZip
if ($OutDir -and -not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir | Out-Null
}

if (Test-Path $OutZip) {
    Remove-Item $OutZip -Force
}

Write-Host "Packaging unsigned Windows app..."
if (Test-Path $AppDir) {
    Compress-Archive -Path "$AppDir\*" -DestinationPath $OutZip -CompressionLevel Optimal
} else {
    Compress-Archive -Path $AppExe -DestinationPath $OutZip -CompressionLevel Optimal
}

Write-Host "Created: $OutZip"
