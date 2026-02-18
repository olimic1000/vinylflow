#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_PATH="${APP_PATH:-dist/VinylFlow.app}"
OUT_ZIP="${OUT_ZIP:-dist/VinylFlow-macos-unsigned.zip}"

if [[ ! -d "$APP_PATH" ]]; then
  echo "Error: app bundle not found at $APP_PATH"
  echo "Build first with: bash scripts/build_desktop_macos.sh"
  exit 1
fi

mkdir -p "$(dirname "$OUT_ZIP")"
rm -f "$OUT_ZIP"

echo "Packaging unsigned app..."
ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$OUT_ZIP"

echo "Created: $OUT_ZIP"
