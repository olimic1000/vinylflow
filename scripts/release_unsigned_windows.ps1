param(
    [Parameter(Mandatory = $true)]
    [string]$Tag,

    [switch]$SkipDraft
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RootDir

Write-Host "Step 1/3: Build Windows app bundle"
powershell -ExecutionPolicy Bypass -File ".\scripts\build_desktop_windows.ps1"

Write-Host "Step 2/3: Package unsigned zip"
powershell -ExecutionPolicy Bypass -File ".\scripts\package_unsigned_windows.ps1"

if ($SkipDraft) {
    Write-Host "Step 3/3: Skipped draft release creation (-SkipDraft)"
    Write-Host "Artifact ready: dist/VinylFlow-windows-unsigned.zip"
    exit 0
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Error "GitHub CLI (gh) not found. Install gh or rerun with -SkipDraft."
}

$zipPath = if ($env:ZIP_PATH) { $env:ZIP_PATH } else { "dist/VinylFlow-windows-unsigned.zip" }
$notesFile = if ($env:RELEASE_NOTES_FILE) { $env:RELEASE_NOTES_FILE } else { ".github/RELEASE_TEMPLATE.md" }
$title = if ($env:RELEASE_TITLE) { $env:RELEASE_TITLE } else { "$Tag - Windows unsigned beta" }

if (-not (Test-Path $zipPath)) {
    Write-Error "Release artifact not found at $zipPath"
}

if (-not (Test-Path $notesFile)) {
    Write-Error "Release notes file not found at $notesFile"
}

Write-Host "Step 3/3: Create GitHub draft release"
gh release create $Tag $zipPath --title $title --notes-file $notesFile --draft

Write-Host "Done: unsigned Windows beta release flow complete for $Tag"
