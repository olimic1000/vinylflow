#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TAG="${1:-}"
SKIP_DRAFT="${SKIP_DRAFT:-0}"

if [[ -z "$TAG" ]]; then
  echo "Usage: bash scripts/release_unsigned_macos.sh <tag>"
  echo "Example: bash scripts/release_unsigned_macos.sh v0.2.0-beta1"
  echo "Set SKIP_DRAFT=1 to skip GitHub draft release creation."
  exit 1
fi

echo "Step 1/3: Build macOS app bundle"
bash scripts/build_desktop_macos.sh

echo "Step 2/3: Package unsigned zip"
bash scripts/package_unsigned_macos.sh

if [[ "$SKIP_DRAFT" == "1" ]]; then
  echo "Step 3/3: Skipped draft release creation (SKIP_DRAFT=1)"
  echo "Artifact ready: dist/VinylFlow-macos-unsigned.zip"
  exit 0
fi

echo "Step 3/3: Create GitHub draft release"
bash scripts/draft_unsigned_release.sh "$TAG"

echo "Done: unsigned beta release flow complete for $TAG"
