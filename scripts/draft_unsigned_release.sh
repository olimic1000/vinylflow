#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: GitHub CLI (gh) is not installed."
  exit 1
fi

TAG="${1:-}"
if [[ -z "$TAG" ]]; then
  echo "Usage: bash scripts/draft_unsigned_release.sh <tag>"
  echo "Example: bash scripts/draft_unsigned_release.sh v0.2.0-beta1"
  exit 1
fi

ZIP_PATH="${ZIP_PATH:-dist/VinylFlow-macos-unsigned.zip}"
TITLE="${RELEASE_TITLE:-$TAG - macOS unsigned beta}"
NOTES_FILE="${RELEASE_NOTES_FILE:-.github/RELEASE_TEMPLATE.md}"

if [[ ! -f "$ZIP_PATH" ]]; then
  echo "Error: release artifact not found at $ZIP_PATH"
  echo "Create it with: bash scripts/package_unsigned_macos.sh"
  exit 1
fi

if [[ ! -f "$NOTES_FILE" ]]; then
  echo "Error: release notes file not found at $NOTES_FILE"
  exit 1
fi

echo "Creating draft release: $TAG"
gh release create "$TAG" \
  "$ZIP_PATH" \
  --title "$TITLE" \
  --notes-file "$NOTES_FILE" \
  --draft

echo "Draft release created for $TAG"
